"""fio-test.py — CLI utility for NVMe/SAS/SATA disk performance testing.

Usage:
    python fio-test.py                  # Dry-run mode
    python fio-test.py -c               # Confirm and run tests
    python fio-test.py -c -s            # Sequential mode for VMs
    python fio-test.py -c -p            # With preconditioning
    python fio-test.py -c -o results    # Custom output path
    python fio-test.py -c -r 60         # Custom runtime in seconds
    python fio-test.py -c --threshold-nvme "seq_read=150000:seq_write=120000"
"""

import argparse
import concurrent.futures
import json
import subprocess
import sys

from rich.console import Console
from rich.table import Table

from configs import nvme, sas, sata
from utils.reporter import generate_report
from utils.scanner import scan_disks

console = Console(color_system=None, highlight=False)

INTERFACE_CONFIGS = {
    "nvme": nvme.NVME_CONFIG,
    "sas": sas.SAS_CONFIG,
    "sata": sata.SATA_CONFIG,
}

INTERFACE_DESCRIPTIONS = {
    "nvme": "NVMe (PCIe)",
    "sas": "SAS",
    "sata": "SATA",
}

INTERFACE_THRESHOLDS = {
    "nvme": {
        "seq_read": 150000,
        "seq_write": 120000,
        "rand_read": 100000,
        "rand_write": 80000,
    },
    "sas": {
        "seq_read": 15000,
        "seq_write": 14000,
        "rand_read": 12000,
        "rand_write": 10000,
    },
    "sata": {
        "seq_read": 12000,
        "seq_write": 11000,
        "rand_read": 10000,
        "rand_write": 8000,
    },
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="NVMe/SAS/SATA disk performance testing via fio",
        epilog=(
            "Examples:\n"
            "  python fio-test.py                     # Dry-run\n"
            "  python fio-test.py -c                  # Run with default settings\n"
            "  python fio-test.py -c -s               # Sequential mode\n"
            "  python fio-test.py -c -p               # With preconditioning\n"
            "  python fio-test.py -c -o results       # Custom output dir\n"
            "  python fio-test.py -c -r 60            # 60s runtime\n"
            "  python fio-test.py -c --threshold-nvme \"seq_read=200000\""
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", "--confirm", action="store_true", help="Confirm test launch; without flag dry-run"
    )
    parser.add_argument(
        "-s", "--sequential", action="store_true", help="Sequential mode for VMs"
    )
    parser.add_argument(
        "-p", "--precond", action="store_true", help="Preconditioning"
    )
    parser.add_argument(
        "-o", "--output", type=str, default=None, help="Output path"
    )
    parser.add_argument(
        "-r", "--runtime", type=int, default=30, help="Test duration in seconds (default: 30)"
    )
    parser.add_argument("--threshold-nvme", type=str, default=None)
    parser.add_argument("--threshold-sas", type=str, default=None)
    parser.add_argument("--threshold-sata", type=str, default=None)
    return parser.parse_args()


def check_threshold(test_id, res, thresholds):
    thr = thresholds.get(test_id)
    if thr is None or "error" in res:
        return "unknown"
    if res.get("iops", 0) >= thr:
        return "done"
    return "fail"


def parse_custom_thresholds(raw):
    result = {}
    if not raw:
        return result
    for part in raw.split(":"):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = int(v.strip())
    return result


def validate_configs():
    for name, cfg in INTERFACE_CONFIGS.items():
        desc = INTERFACE_DESCRIPTIONS.get(name, name)
        if cfg is None:
            console.print(f"[bold red]ERROR:[/bold red] {desc} config is None")
            sys.exit(1)
        if not isinstance(cfg, dict):
            console.print(f"[bold red]ERROR:[/bold red] {desc} config is not a dict")
            sys.exit(1)


def run_fio_test(disk_info, test_id, base_args):
    disk_path = disk_info["path"]
    fio_args = list(base_args)
    cmd = [
        "fio",
        "--name", test_id,
        "--filename", disk_path,
        "--direct=1",
        "--ioengine=libaio",
        "--group_reporting",
        "--norandommap",
        "--output-format=json",
    ] + fio_args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError:
        return {"error": "fio not found"}
    except Exception as exc:
        return {"error": str(exc)}

    if result.returncode != 0:
        hint = (result.stderr or "").strip()[:200]
        return {"error": f"fio failed (rc={result.returncode}): {hint}"}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"JSON parse error: {exc}"}

    jobs = data.get("jobs")
    if not jobs:
        return {"error": "no jobs in fio output"}

    job = jobs[0]
    mode = job.get("read" if "read" in test_id else "write", {})
    stats = mode
    if not stats:
        return {"error": f"missing stats for {test_id}"}

    iops = stats.get("iops", 0)
    bw_bytes = stats.get("bw_bytes", 0)
    lat = stats.get("lat_ns", {})
    lat_avg = lat.get("mean", 0) / 1e6
    lat_p99 = lat.get("percentile", {}).get("99.000000", 0) / 1e6
    bw_mb = bw_bytes / (1024 * 1024)

    return {"iops": iops, "bw_mb": bw_mb, "lat_avg": lat_avg, "lat_p99": lat_p99}


def run_precondition(disk_info):
    disk_path = disk_info["path"]
    cmd = [
        "fio",
        "--name=precond",
        "--filename", disk_path,
        "--direct=1",
        "--ioengine=libaio",
        "--rw=write",
        "--bs=128k",
        "--size=100%",
        "--numjobs=1",
        "--group_reporting",
        "--output-format=json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        return result.returncode == 0
    except Exception:
        return False


def optimize_nvme_args(test_id, args_list, pcie_info):
    if not pcie_info:
        return args_list

    gen = pcie_info.get("generation")
    if gen is None:
        return args_list

    def set_arg(args, key, value):
        new_args = []
        skip_next = False
        i = 0
        while i < len(args):
            if skip_next:
                skip_next = False
                i += 1
                continue
            arg = args[i]
            if arg == key:
                new_args.append(key)
                new_args.append(str(value))
                i += 1
                continue
            if arg.startswith(f"{key}="):
                new_args.append(f"{key}={value}")
                i += 1
                continue
            new_args.append(arg)
            if arg == key:
                skip_next = True
            i += 1
        return new_args

    new_args = list(args_list)

    if gen >= 5:
        if test_id == "seq_read":
            new_args = set_arg(new_args, "--numjobs", 4)
            new_args = set_arg(new_args, "--iodepth", 16)
            new_args = set_arg(new_args, "--bs", "256k")
        elif test_id == "seq_write":
            new_args = set_arg(new_args, "--numjobs", 2)
            new_args = set_arg(new_args, "--iodepth", 16)
        elif test_id == "rand_read":
            new_args = set_arg(new_args, "--numjobs", 16)
            new_args = set_arg(new_args, "--iodepth", 16)
        elif test_id == "rand_write":
            new_args = set_arg(new_args, "--numjobs", 8)
            new_args = set_arg(new_args, "--iodepth", 16)
    elif gen == 4:
        if test_id == "seq_read":
            new_args = set_arg(new_args, "--numjobs", 2)
            new_args = set_arg(new_args, "--iodepth", 16)
        elif test_id == "rand_read":
            new_args = set_arg(new_args, "--numjobs", 8)
            new_args = set_arg(new_args, "--iodepth", 32)

    return new_args


try:
    from rich.box import HLINE_BOX as _HLINE_BOX
except ImportError:
    _HLINE_BOX = None


def _status_style(status):
    if status == "done":
        return "[bold green]done[/bold green]"
    elif status == "fail":
        return "[bold red]fail[/bold red]"
    elif status == "undone":
        return "[bold yellow]undone[/bold yellow]"
    return "[dim]unknown[/dim]"


def build_results_table(disks, results):
    outer = Table(
        title="Test Results",
        show_header=True,
        box=None,
    )
    outer.add_column("№", justify="right", style="bold")
    outer.add_column("Накопитель", min_width=30)
    outer.add_column("Результаты тестирования", min_width=70)

    for idx, disk in enumerate(disks, 1):
        di = disk.get("disk", {})
        disk_name = di.get("name", "unknown")
        model = di.get("model", "N/A").strip()
        tran = di.get("tran", "N/A")
        serial = di.get("serial", "N/A").strip()
        slot = disk.get("slot", "N/A")
        size = di.get("size", "N/A")

        pcie = disk.get("pcie", {})
        pcie_str = ""
        if pcie:
            gen = pcie.get("generation")
            width = pcie.get("width")
            if gen and width:
                pcie_str = f"PCIe Gen{gen} x{width}"

        disk_info_text = (
            f"/dev/{disk_name} | {model} | {tran}"
            + (f" ({pcie_str})" if pcie_str else "")
            + f" | SN: {serial} | Slot: {slot} | {size}"
        )

        inner = Table(box=_HLINE_BOX, show_header=True, show_edge=False)
        inner.add_column("Test")
        inner.add_column("Block")
        inner.add_column("IOPS", justify="right")
        inner.add_column("Speed", justify="right")
        inner.add_column("Lat Avg", justify="right")
        inner.add_column("Lat P99", justify="right")
        inner.add_column("Status", justify="center")

        disk_results = results.get(idx - 1, {})
        test_order = ["seq_read", "seq_write", "rand_read", "rand_write"]
        for test_id in test_order:
            res = disk_results.get(test_id, {})
            if "error" in res:
                inner.add_row(
                    test_id, "-", "-", "-", "-", "-", _status_style("undone")
                )
            else:
                iops = f"{res.get('iops', 0):.0f}"
                bw = f"{res.get('bw_mb', 0):.1f} MB/s"
                lat_avg = f"{res.get('lat_avg', 0):.2f} ms"
                lat_p99 = f"{res.get('lat_p99', 0):.2f} ms"
                status = res.get("status", "unknown")
                inner.add_row(
                    test_id, res.get("bs", "4k"), iops, bw, lat_avg, lat_p99,
                    _status_style(status)
                )

        outer.add_row(str(idx), disk_info_text, inner)

    return outer


def process_task_result(results, idx, disk, t, fio_args, res):
    bs = "4k"
    for i, a in enumerate(fio_args):
        if a == "--bs" and i + 1 < len(fio_args):
            bs = fio_args[i + 1]
            break
        if a.startswith("--bs="):
            bs = a.split("=", 1)[1]
            break

    if "error" in res:
        results[idx][t] = {"error": res["error"], "status": "undone", "bs": bs}
        disk_name = disk.get("disk", {}).get("name", "unknown")
        console.print(
            f"  [bold red]ERROR[/bold red] {disk_name}/{t}: {res['error']}"
        )
        return

    thresholds = results[idx].get("_thresholds", {})
    status = check_threshold(t, res, thresholds)
    res["status"] = status
    res["bs"] = bs
    results[idx][t] = res


def main():
    args = parse_args()
    validate_configs()

    thresholds = dict(INTERFACE_THRESHOLDS)
    if args.threshold_nvme:
        thresholds["nvme"] = {**thresholds["nvme"], **parse_custom_thresholds(args.threshold_nvme)}
    if args.threshold_sas:
        thresholds["sas"] = {**thresholds["sas"], **parse_custom_thresholds(args.threshold_sas)}
    if args.threshold_sata:
        thresholds["sata"] = {**thresholds["sata"], **parse_custom_thresholds(args.threshold_sata)}

    console.print("[bold]Scanning disks...[/bold]")

    try:
        system_disks, disks = scan_disks(INTERFACE_CONFIGS)
    except Exception as exc:
        console.print(f"[bold red]Scan error:[/bold red] {exc}")
        sys.exit(1)

    if system_disks:
        console.print("\n[bold]System disks:[/bold]")
        for sd in system_disks:
            root_info = sd.get("root_partition", "")
            name = sd.get("disk", {}).get("name", "?")
            console.print(
                f"  [yellow]/dev/{name}[/yellow]"
                + (f" (root: {root_info})" if root_info else "")
            )

    console.print("\n[bold]Target disks:[/bold]")
    for i, d in enumerate(disks, 1):
        di = d.get("disk", {})
        model = di.get("model", "N/A").strip()
        name = di.get("name", "unknown")
        console.print(f"  [green]{i}. /dev/{name}[/green] {model}")

    if not args.confirm:
        console.print(
            "\n[bold yellow]Dry-run mode.[/bold yellow] "
            "Use -c flag to confirm test launch."
        )
        sys.exit(0)

    if args.precond:
        console.print("\n[bold]Preconditioning...[/bold]")
        for d in disks:
            disk_name = d.get("disk", {}).get("name", "unknown")
            console.print(f"  Preconditioning /dev/{disk_name}...")
            ok = run_precondition(d)
            if ok:
                console.print(f"  [green]Done[/green] /dev/{disk_name}")
            else:
                console.print(f"  [red]Failed[/red] /dev/{disk_name}")

    if args.runtime != 30:
        console.print(f"\n[bold]Runtime: {args.runtime}s per test[/bold]")

    mode = "sequential" if args.sequential else "parallel"
    console.print(f"\n[bold]Mode: {mode}[/bold]")

    test_ids = ["seq_read", "seq_write", "rand_read", "rand_write"]
    base_fio_args = [
        "--runtime", str(args.runtime),
        "--rw", None,
        "--bs", None,
        "--numjobs", None,
        "--iodepth", None,
    ]

    test_configs = {
        "seq_read": {"rw": "read", "bs": "128k", "numjobs": "1", "iodepth": "1"},
        "seq_write": {"rw": "write", "bs": "128k", "numjobs": "1", "iodepth": "1"},
        "rand_read": {"rw": "randread", "bs": "4k", "numjobs": "1", "iodepth": "1"},
        "rand_write": {"rw": "randwrite", "bs": "4k", "numjobs": "1", "iodepth": "1"},
    }

    results = [{} for _ in disks]
    tasks = []

    for disk_idx, disk in enumerate(disks):
        tran = disk.get("disk", {}).get("tran", "")
        pcie_info = disk.get("pcie") if tran and "nvme" in tran.lower() else None

        for t in test_ids:
            cfg = test_configs[t]
            fio_args = [
                "--runtime", str(args.runtime),
                "--rw", cfg["rw"],
                "--bs", cfg["bs"],
                "--numjobs", cfg["numjobs"],
                "--iodepth", cfg["iodepth"],
            ]

            if tran and "nvme" in tran.lower() and pcie_info:
                fio_args = optimize_nvme_args(t, fio_args, pcie_info)

            thresholds_key = "nvme" if (tran and "nvme" in tran.lower()) else (
                "sas" if (tran and "sas" in tran.lower()) else "sata"
            )
            results[disk_idx]["_thresholds"] = thresholds.get(thresholds_key, {})
            tasks.append((disk_idx, disk, t, fio_args))

    if args.sequential:
        for disk_idx, disk, t, fio_args in tasks:
            disk_name = disk.get("disk", {}).get("name", "unknown")
            console.print(f"  {disk_name}/{t}...")
            res = run_fio_test(disk, t, fio_args)
            process_task_result(results, disk_idx, disk, t, fio_args, res)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            future_map = {}
            for disk_idx, disk, t, fio_args in tasks:
                fut = pool.submit(run_fio_test, disk, t, fio_args)
                future_map[fut] = (disk_idx, disk, t, fio_args)

            for fut in concurrent.futures.as_completed(future_map):
                disk_idx, disk, t, fio_args = future_map[fut]
                res = fut.result()
                process_task_result(results, disk_idx, disk, t, fio_args, res)

    table = build_results_table(disks, results)
    console.print()
    console.print(table)

    generate_report(disks, results, output_path=args.output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted.[/bold yellow]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"\n[bold red]Fatal error:[/bold red] {exc}")
        sys.exit(1)
