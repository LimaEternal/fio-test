"""
fio-test.py — Автоматический бенчмаркинг несистемных накопителей.

Сканирует систему на несистемные диски, классифицирует их по интерфейсу
(NVMe/SAS/SATA), запускает FIO-тесты с оптимальными параметрами для каждого типа
и выводит результаты в реальном времени через rich, а по завершении — в MD-отчёт.

Использование:
    python fio-test.py
    python fio-test.py --precond
    python fio-test.py --output report.md
"""

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from configs import nvme, sas, sata
from utils.reporter import generate_report
from utils.scanner import get_non_system_disks

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
            "  python fio-test.py                  — базовое тестирование\n"
            "  python fio-test.py --precond         — с прекондишнингом\n"
            "  python fio-test.py --output my.md    — свой путь для отчёта\n"
            "  python fio-test.py --threshold-nvme 2000,1500,100000,80000\n"
        ),
    )

    parser.add_argument(
        "--precond",
        action="store_true",
        help=(
            "Выполнить прекондишнинг (запись 100%% объёма диска перед тестами). "
            "Стабилизирует производительность SSD, но затирает все данные. "
            "Запись идёт блоком bs=1M напрямую на устройство (--direct=1)."
        ),
    )

    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Путь для MD-отчёта (по умолчанию: reports/fio_report_<timestamp>.md)",
    )

    parser.add_argument(
        "--runtime",
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


def run_fio_test(disk_info: dict, test_id: str, base_args: list[str]) -> dict:
    """Запускает один подтест FIO и парсит JSON-результат."""
    disk_path = disk_info["path"]
    sector_size = disk_info["phy_sec"]

    fio_args = list(base_args)

    if sector_size == 4096:
        if "rand" in test_id:
            fio_args = [
                a if not a.startswith("--bs=") else "--bs=4k" for a in fio_args
            ]
        elif "seq" in test_id:
            fio_args = [
                a if not a.startswith("--bs=") else "--bs=128k" for a in fio_args
            ]

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


def build_table(results: list[dict]) -> Table:
    """Строит Rich-таблицу с результатами."""
    table = Table(
        title="[bold green]Результаты тестирования накопителей (FIO)[/bold green]",
        show_lines=True,
    )

    table.add_column("Диск", style="cyan")
    table.add_column("Модель / Серийник", style="magenta")
    table.add_column("Интерфейс / Объём")
    table.add_column("Профиль теста", style="yellow")
    table.add_column("IOPS", justify="right", style="green")
    table.add_column("Скорость (МБ/с)", justify="right", style="green")
    table.add_column("Lat Avg (мс)", justify="right")
    table.add_column("Lat p99 (мс)", justify="right")
    table.add_column("Статус", justify="center")

    for r in results:
        status = r.get("status", "...")
        if status == "done":
            status_style = "[bold green]done[/bold green]"
        elif status == "undone":
            status_style = "[bold red]undone[/bold red]"
        else:
            status_style = status

        table.add_row(
            r["disk"],
            f"{r['model']}\n[grey50]SN: {r['serial']}[/grey50]",
            f"{r['tran']} (Sector: {r['sector']}B)\n[grey50]{r['size']}[/grey50]",
            r["test_name"],
            str(r["iops"]),
            str(r["bw"]),
            str(r["lat_avg"]),
            str(r["lat_p99"]),
            status_style,
        )

    return table


def main() -> None:
    args = parse_args()

    thresholds = dict(INTERFACE_THRESHOLDS)

    if args.threshold_nvme:
        thresholds["NVME"] = parse_custom_thresholds(args.threshold_nvme)
    if args.threshold_sas:
        thresholds["SAS"] = parse_custom_thresholds(args.threshold_sas)
    if args.threshold_sata:
        thresholds["SATA"] = parse_custom_thresholds(args.threshold_sata)

    console.print("[bold blue]Сканирование системы на несистемные диски...[/bold blue]")
    disks = get_non_system_disks(INTERFACE_CONFIGS)

    if not disks:
        console.print(
            "[bold red]Безопасные несистемные диски для тестов не найдены![/bold red]"
        )
        sys.exit(1)

    console.print(
        f"Обнаружено целевых дисков: [bold green]{len(disks)}[/bold green]\n"
    )

    for d in disks:
        desc = INTERFACE_DESCRIPTIONS.get(d["tran"], "")
        console.print(f"  [cyan]/dev/{d['name']}[/cyan] — {d['model']} ({d['tran']})")
        if desc:
            console.print(f"    [grey50]{desc}[/grey50]")
    console.print()

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

    total_steps = len(tasks)

    with Live(
        build_table(results), refresh_per_second=4, console=console
    ) as live:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
        )
        overall = progress.add_task(
            "[cyan]Выполнение бенчмарка...", total=total_steps
        )

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

                live.update(build_table(results))
                progress.advance(overall)

    console.print(
        "\n[bold green]Все тесты завершены.[/bold green]"
    )

    report_path = generate_report(disks, results, args.output)
    console.print(
        f"[bold green]Отчёт сохранён: {report_path}[/bold green]"
    )


if __name__ == "__main__":
    main()
