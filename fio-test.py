"""
fio-test.py — Автоматический бенчмаркинг несистемных накопителей.

Сканирует систему на несистемные диски, классифицирует их по интерфейсу
(NVMe/SAS/SATA), запускает FIO-тесты с оптимальными параметрами для каждого типа
и выводит результаты в реальном времени через rich, а по завершении — в MD-отчёт.

Использование:
    python fio-test.py              — сканирование (dry-run)
    python fio-test.py -c           — тестирование
    python fio-test.py -c -p        — с прекондишнингом
    python fio-test.py -c -r 60     — 60 сек на тест
    python fio-test.py -c -o my.md  — свой путь отчёта
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

console = Console()

INTERFACE_CONFIGS = {
    "NVME": nvme.TESTS,
    "SAS": sas.TESTS,
    "SATA": sata.TESTS,
}

INTERFACE_DESCRIPTIONS = {
    "NVME": nvme.DESCRIPTION,
    "SAS": sas.DESCRIPTION,
    "SATA": sata.DESCRIPTION,
}

INTERFACE_THRESHOLDS = {
    "NVME": nvme.THRESHOLDS,
    "SAS": sas.THRESHOLDS,
    "SATA": sata.THRESHOLDS,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Автоматический бенчмаркинг несистемных накопителей (FIO)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python fio-test.py                  — сканирование (dry-run)\n"
            "  python fio-test.py -c               — тестирование\n"
            "  python fio-test.py -c -p             — с прекондишнингом\n"
            "  python fio-test.py -c -r 60          — 60 сек на тест\n"
            "  python fio-test.py -c -o my.md       — свой путь отчёта\n"
            "  python fio-test.py -c --threshold-nvme 5000,3000,500000,200000\n"
        ),
    )

    parser.add_argument(
        "-c", "--confirm",
        action="store_true",
        help="Подтвердить запуск тестов. Без этого флага скрипт только покажет диски и выйдет.",
    )

    parser.add_argument(
        "-p", "--precond",
        action="store_true",
        help=(
            "Выполнить прекондишнинг (запись 100%% объёма диска перед тестами). "
            "Стабилизирует производительность SSD, но затирает все данные. "
            "Запись идёт блоком bs=1M напрямую на устройство (--direct=1)."
        ),
    )

    parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Путь для MD-отчёта (по умолчанию: reports/fio_report_<timestamp>.md)",
    )

    parser.add_argument(
        "-r", "--runtime",
        type=int,
        default=30,
        help="Длительность каждого теста в секундах (по умолчанию: 30)",
    )

    parser.add_argument(
        "--threshold-nvme",
        type=str,
        default=None,
        help="Свои пороги для NVMe: seq_read_bw,seq_write_bw,rand_read_iops,rand_write_iops (через запятую)",
    )

    parser.add_argument(
        "--threshold-sas",
        type=str,
        default=None,
        help="Свои пороги для SAS: seq_read_bw,seq_write_bw,rand_read_iops,rand_write_iops (через запятую)",
    )

    parser.add_argument(
        "--threshold-sata",
        type=str,
        default=None,
        help="Свои пороги для SATA: seq_read_bw,seq_write_bw,rand_read_iops,rand_write_iops (через запятую)",
    )

    return parser.parse_args()


def check_threshold(test_id: str, res: dict, thresholds: dict) -> str:
    """Проверяет, прошёл ли тест пороговое значение. Возвращает 'done' или 'undone'."""
    if "error" in res:
        return "undone"

    t = thresholds.get(test_id, {})

    if "min_bw_mb" in t:
        if res["bw_mb"] < t["min_bw_mb"]:
            return "undone"

    if "min_iops" in t:
        if res["iops"] < t["min_iops"]:
            return "undone"

    return "done"


def parse_custom_thresholds(raw: str) -> dict:
    """
    Парсит строку с порогами: seq_read_bw,seq_write_bw,rand_read_iops,rand_write_iops
    Возвращает словарь THRESHOLDS.
    """
    vals = [x.strip() for x in raw.split(",")]
    if len(vals) != 4:
        console.print("[red]Ошибка: нужно ровно 4 значения через запятую[/red]")
        sys.exit(1)

    try:
        seq_read_bw, seq_write_bw, rand_read_iops, rand_write_iops = [float(v) for v in vals]
    except ValueError:
        console.print("[red]Ошибка: все значения должны быть числами[/red]")
        sys.exit(1)

    return {
        "seq_read":  {"min_bw_mb": seq_read_bw},
        "seq_write": {"min_bw_mb": seq_write_bw},
        "rand_read": {"min_iops": rand_read_iops},
        "rand_write": {"min_iops": rand_write_iops},
    }


def validate_configs() -> None:
    """Валидация конфигов FIO-тестов. Вызывается при старте до сканирования дисков."""
    if not isinstance(INTERFACE_CONFIGS, dict):
        console.print("[red]INTERFACE_CONFIGS должен быть словарём[/red]")
        sys.exit(1)

    required_test_keys = {"id", "name", "args"}
    required_args = {"--rw=", "--bs=", "--runtime=", "--time_based"}
    valid_bs = {"512", "1k", "2k", "4k", "8k", "16k", "32k", "64k", "128k",
                "256k", "512k", "1m"}

    for iface, tests in INTERFACE_CONFIGS.items():
        if not isinstance(tests, list) or len(tests) == 0:
            console.print(f"[red]Ошибка в конфиге {iface}: TESTS должен быть непустым списком[/red]")
            sys.exit(1)

        for idx, test in enumerate(tests):
            missing = required_test_keys - set(test.keys())
            if missing:
                console.print(f"[red]Ошибка в конфиге {iface}, тест {idx}: отсутствуют ключи {missing}[/red]")
                sys.exit(1)

            args = test["args"]
            if not isinstance(args, list):
                console.print(f"[red]Ошибка в конфиге {iface}, тест {test['id']}: args должен быть списком[/red]")
                sys.exit(1)

            for arg in args:
                if not any(arg.startswith(req) for req in required_args):
                    if arg in ("--time_based",):
                        continue
                    if not arg.startswith("--") and not arg.startswith("-"):
                        console.print(
                            f"[red]Ошибка в конфиге {iface}, тест {test['id']}: "
                            f"неожиданный аргумент '{arg}'[/red]"
                        )
                        sys.exit(1)

            bs_val = None
            for arg in args:
                if arg.startswith("--bs="):
                    bs_val = arg.split("=", 1)[1].lower()
                    break

            if bs_val is None:
                console.print(
                    f"[red]Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"отсутствует параметр --bs[/red]"
                )
                sys.exit(1)

            if bs_val not in valid_bs:
                console.print(
                    f"[red]Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"некорректный bs='{bs_val}'[/red]"
                )
                sys.exit(1)

            if not any(a.startswith("--runtime=") for a in args):
                console.print(
                    f"[red]Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"отсутствует --runtime[/red]"
                )
                sys.exit(1)

            if "--time_based" not in args:
                console.print(
                    f"[red]Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"отсутствует --time_based[/red]"
                )
                sys.exit(1)

    for iface, thresholds in INTERFACE_THRESHOLDS.items():
        if not isinstance(thresholds, dict):
            console.print(f"[red]Ошибка в конфиге {iface}: THRESHOLDS должен быть словарём[/red]")
            sys.exit(1)

        if iface not in INTERFACE_CONFIGS:
            console.print(f"[red]Ошибка в конфиге {iface}: интерфейс не найден в INTERFACE_CONFIGS[/red]")
            sys.exit(1)

        test_ids = {t["id"] for t in INTERFACE_CONFIGS[iface]}
        for tid, vals in thresholds.items():
            if tid not in test_ids:
                console.print(
                    f"[red]Ошибка в THRESHOLDS[{iface}]: тест '{tid}' не найден в TESTS[/red]"
                )
                sys.exit(1)
            if "min_bw_mb" not in vals and "min_iops" not in vals:
                console.print(
                    f"[red]Ошибка в THRESHOLDS[{iface}]['{tid}']: "
                    f"нужен min_bw_mb или min_iops[/red]"
                )
                sys.exit(1)

        if len(thresholds) != len(test_ids):
            console.print(
                f"[red]Ошибка в THRESHOLDS[{iface}]: "
                f"количество порогов ({len(thresholds)}) != количество тестов ({len(test_ids)})[/red]"
            )
            sys.exit(1)

    for iface, desc in INTERFACE_DESCRIPTIONS.items():
        if not isinstance(desc, str) or not desc.strip():
            console.print(f"[red]Ошибка в конфиге {iface}: DESCRIPTION должен быть непустой строкой[/red]")
            sys.exit(1)


def run_fio_test(disk_info: dict, test_id: str, base_args: list[str]) -> dict:
    """Запускает один подтест FIO и парсит JSON-результат."""
    disk_path = disk_info["path"]
    fio_args = list(base_args)

    cmd = [
        "fio",
        f"--name={test_id}",
        f"--filename={disk_path}",
        "--direct=1",
        "--ioengine=libaio",
        "--group_reporting",
        "--norandommap",
        "--output-format=json",
    ] + fio_args

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"error": f"FIO Error (exit {res.returncode})"}

    try:
        fio_data = json.loads(res.stdout)
        job = fio_data["jobs"][0]

        mode = "read" if "read" in test_id else "write"
        stats = job[mode]

        avg_lat = stats["lat_ns"]["mean"] / 1_000_000
        p99_lat = stats["clat_ns"]["percentile"]["99.000000"] / 1_000_000

        return {
            "iops": int(stats["iops"]),
            "bw_mb": round(stats["bw"] / 1024, 2),
            "lat_avg": round(avg_lat, 2),
            "lat_p99": round(p99_lat, 2),
        }
    except Exception as e:
        return {"error": f"Parse Error: {e}"}


def run_precondition(disk_info: dict) -> bool:
    """Выполняет прекондишнинг — запись 100% объёма диска."""
    cmd = [
        "fio",
        f"--name=precond",
        f"--filename={disk_info['path']}",
        "--size=100%",
        "--rw=write",
        "--bs=1M",
        "--direct=1",
        "--ioengine=libaio",
        "--numjobs=1",
        "--iodepth=32",
        "--group_reporting",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


# Custom box for inner table — horizontal lines only, no vertical separators
# Try multiple approaches for compatibility with different rich versions
_HLINE_BOX = None
try:
    from rich.box import SIMPLE as _HLINE_BOX
except (ImportError, Exception):
    pass


def _status_style(status: str) -> str:
    """Возвращает styled строку статуса."""
    if status == "done":
        return "[bold green]done[/bold green]"
    elif status == "undone":
        return "[bold red]undone[/bold red]"
    return status


def build_results_table(disks: list, results: list) -> Table:
    """Строит внешнюю таблицу с вложенными таблицами для каждого диска."""
    # Group results by disk (preserving order)
    grouped = []
    for disk in disks:
        disk_results = [r for r in results if r["disk"] == disk["name"]]
        if disk_results:
            grouped.append((disk, disk_results))

    outer = Table(
        show_header=False,
        show_edge=True,
        show_lines=True,
        padding=(0, 1),
    )
    outer.add_column("№", justify="center", width=3)
    outer.add_column("")

    for disk_num, (disk, disk_results) in enumerate(grouped, 1):
        inner = Table(
            box=_HLINE_BOX,
            show_edge=False,
            show_lines=True,
            padding=(0, 2),
        )
        inner.add_column("Профиль теста", style="yellow")
        inner.add_column("Блок", justify="right")
        inner.add_column("IOPS", justify="right", style="green")
        inner.add_column("Скорость (МБ/с)", justify="right", style="green")
        inner.add_column("Lat Avg (мс)", justify="right")
        inner.add_column("Lat p99 (мс)", justify="right")
        inner.add_column("Статус", justify="center")

        for r in disk_results:
            inner.add_row(
                r["test_name"],
                str(r.get("bs", "—")),
                str(r["iops"]),
                str(r["bw"]),
                str(r["lat_avg"]),
                str(r["lat_p99"]),
                _status_style(r.get("status", "...")),
            )

        outer.add_row(str(disk_num), inner)

    return outer


def main() -> None:
    args = parse_args()
    validate_configs()

    thresholds = dict(INTERFACE_THRESHOLDS)

    if args.threshold_nvme:
        thresholds["NVME"] = parse_custom_thresholds(args.threshold_nvme)
    if args.threshold_sas:
        thresholds["SAS"] = parse_custom_thresholds(args.threshold_sas)
    if args.threshold_sata:
        thresholds["SATA"] = parse_custom_thresholds(args.threshold_sata)

    console.print("[bold blue]Сканирование системы на несистемные диски...[/bold blue]\n")
    system_disks, disks = scan_disks(INTERFACE_CONFIGS)

    if system_disks:
        console.print("[bold yellow]Системные диски (пропуск):[/bold yellow]")
        for d in system_disks:
            slot_str = f", Slot: {d['slot']}" if d.get("slot") else ""
            console.print(
                f"  [yellow]/dev/{d['name']}[/yellow] — {d['model']} "
                f"({d['tran']}, SN: {d['serial']}{slot_str})"
            )
            console.print(
                f"    └─ [red]/[/red] (корневая ФС)"
            )
        console.print()

    if not disks:
        console.print(
            "[bold red]Безопасные несистемные диски для тестов не найдены![/bold red]"
        )
        sys.exit(1)

    console.print(
        f"[bold green]Целевые диски:[/bold green]"
    )
    for i, d in enumerate(disks, 1):
        slot_str = f", Slot: {d['slot']}" if d.get("slot") else ""
        console.print(
            f"  [cyan]{i}. /dev/{d['name']}[/cyan] — {d['model']} "
            f"({d['tran']}, SN: {d['serial']}{slot_str})"
        )
    console.print()

    if not args.confirm:
        console.print(
            "[yellow]Для запуска тестов добавьте -c (--confirm)[/yellow]"
        )
        sys.exit(0)

    if args.precond:
        console.print(
            "[bold yellow]Прекондишнинг: "
            "запись 100% объёма каждого диска (bs=1M, --direct=1)...[/bold yellow]\n"
        )

        for disk in disks:
            console.print(
                f"  [cyan]Прекондишнинг /dev/{disk['name']}...[/cyan]"
            )
            ok = run_precondition(disk)
            if ok:
                console.print(
                    f"  [green]✓ /dev/{disk['name']} готов[/green]"
                )
            else:
                console.print(
                    f"  [red]✗ Ошибка прекондишинга /dev/{disk['name']}[/red]"
                )
        console.print()

    if args.runtime != 30:
        console.print(
            f"[grey50]Длительность теста: {args.runtime}с[/grey50]\n"
        )

    console.print("[cyan]Выполнение бенчмарка...[/cyan]")

    results = []
    tasks = []

    for disk in disks:
        disk_config = INTERFACE_CONFIGS[disk["tran"]]
        for t in disk_config:
            idx = len(results)
            results.append({
                "disk": disk["name"],
                "model": disk["model"],
                "serial": disk["serial"],
                "tran": disk["tran"],
                "size": disk["size"],
                "sector": disk["phy_sec"],
                "test_name": f"{t['name']}",
                "iops": "...",
                "bw": "...",
                "lat_avg": "...",
                "lat_p99": "...",
                "error_msg": None,
                "bs": "...",
                "status": "...",
            })

            fio_args = list(t["args"])
            if args.runtime != 30:
                fio_args = [
                    (
                        f"--runtime={args.runtime}"
                        if a.startswith("--runtime=")
                        else a
                    )
                    for a in fio_args
                ]

            tasks.append((idx, disk, t, fio_args))

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(disks)
    ) as executor:
        future_to_info = {}
        for idx, disk, t, fio_args in tasks:
            future = executor.submit(
                run_fio_test, disk, t["id"], fio_args
            )
            future_to_info[future] = (idx, disk, t, fio_args)

        for future in concurrent.futures.as_completed(
            future_to_info
        ):
            idx, disk, t, fio_args = future_to_info[future]
            res = future.result()

            bs = "4k"
            for a in fio_args:
                if a.startswith("--bs="):
                    bs = a.split("=", 1)[1]
                    break

            if "error" in res:
                results[idx]["test_name"] = (
                    f"[red]{t['name']}[/red]"
                )
                results[idx]["iops"] = "ERR"
                results[idx]["bw"] = "—"
                results[idx]["lat_avg"] = "—"
                results[idx]["lat_p99"] = "—"
                results[idx]["error_msg"] = res["error"]
                results[idx]["bs"] = bs
                results[idx]["status"] = "undone"
            else:
                results[idx]["test_name"] = (
                    f"[green]{t['name']}[/green]"
                )
                results[idx]["iops"] = f"{res['iops']:,}"
                results[idx]["bw"] = res["bw_mb"]
                results[idx]["lat_avg"] = res["lat_avg"]
                results[idx]["lat_p99"] = res["lat_p99"]
                results[idx]["error_msg"] = None
                results[idx]["bs"] = bs
                results[idx]["status"] = check_threshold(
                    t["id"], res, thresholds[disk["tran"]]
                )

    console.print()
    console.print("[bold green]Результаты тестирования накопителей (FIO)[/bold green]")
    console.print(build_results_table(disks, results))
    console.print(
        "\n[bold green]Все тесты завершены.[/bold green]"
    )

    report_path = generate_report(disks, results, args.output)
    console.print(
        f"[bold green]Отчёт сохранён: {report_path}[/bold green]"
    )


if __name__ == "__main__":
    main()
