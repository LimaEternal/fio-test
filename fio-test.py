"""
fio-test.py — Автоматический бенчмаркинг несистемных накопителей.

Сканирует систему на несистемные диски, классифицирует их по интерфейсу
(NVMe/SAS/SATA), запускает FIO-тесты с оптимальными параметрами для каждого типа
и выводит результаты в консоль + MD-отчёт.

Использование:
    python fio-test.py              — тестирование (параллельно)
    python fio-test.py -s           — тестирование (последовательно)
    python fio-test.py -c           — тестирование с подтверждением
    python fio-test.py -c -s        — с подтверждением, последовательно
    python fio-test.py -c -p        — с подтверждением и прекондишнингом
    python fio-test.py -r 60        — 60 сек на тест
    python fio-test.py -o my.md     — свой путь отчёта
"""

import argparse
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import threading

from rich.console import Console
from rich.table import Table

from configs import nvme, sas, sata
from utils.reporter import generate_report
from utils.scanner import scan_disks

console = Console(color_system=None, highlight=False)

INTERFACE_CONFIGS = {
    "nvme": nvme.TESTS,
    "sas": sas.TESTS,
    "sata": sata.TESTS,
}

TEST_NAMES = {}
for _mod in [nvme, sas, sata]:
    for _test in _mod.TESTS:
        TEST_NAMES[_test["id"]] = _test["name"]

INTERFACE_DESCRIPTIONS = {
    "nvme": nvme.DESCRIPTION,
    "sas": sas.DESCRIPTION,
    "sata": sata.DESCRIPTION,
}

INTERFACE_THRESHOLDS = {
    "nvme": nvme.THRESHOLDS,
    "sas": sas.THRESHOLDS,
    "sata": sata.THRESHOLDS,
}


_SHORT_FLAGS = {"c", "s", "p"}


def _expand_short_flags(argv: list[str]) -> list[str]:
    """Расширяет комбинированные короткие флаги: -sc → -s -c, -cp → -c -p."""
    expanded = []
    for arg in argv:
        if len(arg) > 2 and arg.startswith("-") and not arg.startswith("--") and all(ch in _SHORT_FLAGS for ch in arg[1:]):
            for ch in arg[1:]:
                expanded.append(f"-{ch}")
        else:
            expanded.append(arg)
    return expanded


def parse_args():
    parser = argparse.ArgumentParser(
        description="Тестирование производительности NVMe/SAS/SATA накопителей через fio",
        epilog=(
            "Примеры:\n"
            "  python fio-test.py                           — тестирование (параллельно)\n"
            "  python fio-test.py -s                        — тестирование (последовательно)\n"
            "  python fio-test.py -c                        — с подтверждением перед стартом\n"
            "  python fio-test.py -c -s                     — с подтверждением, последовательно\n"
            "  python fio-test.py -c -p                     — с подтверждением и прекондишнингом\n"
            "  python fio-test.py -r 60                     — 60 секунд на каждый тест\n"
            "  python fio-test.py -o reports/custom.md       — свой путь отчёта\n"
            "  python fio-test.py -c --threshold-nvme \"seq_read=15000:seq_write=12000\""
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", "--confirm", action="store_true",
        help="Запрос подтверждения перед стартом тестов",
    )
    parser.add_argument(
        "-s", "--sequential", action="store_true",
        help="Последовательный режим (для виртуальных машин)",
    )
    parser.add_argument(
        "-p", "--precond", action="store_true",
        help="Прекондишнинг перед тестами",
    )
    parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Путь для выходного MD-отчёта",
    )
    parser.add_argument(
        "-r", "--runtime", type=int, default=30,
        help="Длительность каждого теста в секундах (по умолчанию: 30)",
    )
    parser.add_argument(
        "--threshold-nvme", type=str, default=None,
        help="Пороговые значения NVMe (формат: seq_read=5000:rand_read=500000)",
    )
    parser.add_argument(
        "--threshold-sas", type=str, default=None,
        help="Пороговые значения SAS (формат: seq_read=800:rand_read=30000)",
    )
    parser.add_argument(
        "--threshold-sata", type=str, default=None,
        help="Пороговые значения SATA (формат: seq_read=400:rand_read=10000)",
    )
    return parser.parse_args(_expand_short_flags(sys.argv[1:]))


def check_threshold(test_id, res, thresholds):
    """Проверяет результат теста по пороговым значениям. Возвращает 'done' или 'undone'."""
    thr = thresholds.get(test_id)
    if thr is None or "error" in res:
        return "undone"
    if "min_bw_mb" in thr:
        if res.get("bw_mb", 0) >= thr["min_bw_mb"]:
            return "done"
    elif "min_iops" in thr:
        if res.get("iops", 0) >= thr["min_iops"]:
            return "done"
    return "undone"


def parse_custom_thresholds(raw):
    """Парсит строку порогов вида 'seq_read=5000:rand_read=500000' в словарь."""
    result = {}
    if not raw:
        return result
    for part in raw.split(":"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = int(v.strip())
            if k.startswith("seq_"):
                result.setdefault(k, {})["min_bw_mb"] = v
            elif k.startswith("rand_"):
                result.setdefault(k, {})["min_iops"] = v
    return result


def validate_configs():
    """Валидирует конфигурации всех интерфейсов."""
    required_test_keys = {"id", "name", "args"}
    valid_bs = {"4k", "8k", "16k", "32k", "64k", "128k", "256k", "512k", "1m"}
    required_thresh_keys = {"seq_read", "seq_write", "rand_read", "rand_write"}

    for name, mod in [("nvme", nvme), ("sas", sas), ("sata", sata)]:
        desc = INTERFACE_DESCRIPTIONS.get(name, name)

        # Проверка TESTS
        tests = mod.TESTS
        if not isinstance(tests, list) or len(tests) == 0:
            console.print(f"[red]ОШИБКА:[/red] {desc}: TESTS — пустой список")
            sys.exit(1)
        for t in tests:
            missing = required_test_keys - set(t.keys())
            if missing:
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: тест '{t.get('id', '?')}' "
                    f"не содержит ключей {missing}"
                )
                sys.exit(1)
            for a in t["args"]:
                if a.startswith("--bs="):
                    bs_val = a.split("=", 1)[1].lower()
                    if bs_val not in valid_bs:
                        console.print(
                            f"[red]ОШИБКА:[/red] {desc}: недопустимый bs={bs_val} "
                            f"в тесте {t['id']}"
                        )
                        sys.exit(1)

        # Проверка THRESHOLDS
        thresh = mod.THRESHOLDS
        if not isinstance(thresh, dict):
            console.print(f"[red]ОШИБКА:[/red] {desc}: THRESHOLDS не является словарём")
            sys.exit(1)
        missing_t = required_thresh_keys - set(thresh.keys())
        if missing_t:
            console.print(
                f"[red]ОШИБКА:[/red] {desc}: THRESHOLDS не содержит ключей {missing_t}"
            )
            sys.exit(1)
        for tid, tv in thresh.items():
            if not isinstance(tv, dict):
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: THRESHOLDS['{tid}'] — не словарь"
                )
                sys.exit(1)
            if "min_bw_mb" not in tv and "min_iops" not in tv:
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: THRESHOLDS['{tid}'] — "
                    f"нет min_bw_mb или min_iops"
                )
                sys.exit(1)

        # Проверка DESCRIPTION
        if not mod.DESCRIPTION:
            console.print(f"[red]ОШИБКА:[/red] {desc}: DESCRIPTION пуст")
            sys.exit(1)


def run_fio_test(disk_info, test_id, base_args, cancel_event=None):
    """Запускает fio-тест. Поддерживает отмену через cancel_event."""
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

    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                proc.wait()
                return {"error": "Тест отменён пользователем"}
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue

        stdout, stderr = proc.communicate()
        stdout = stdout.decode() if stdout else ""
        stderr = stderr.decode() if stderr else ""

    except FileNotFoundError:
        return {"error": "Утилита fio не найдена. Установите fio и повторите попытку."}
    except Exception as exc:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        return {"error": str(exc)}

    if proc.returncode != 0:
        hint = stderr.strip()[:200] if stderr else ""
        return {"error": f"fio завершился с кодом {proc.returncode}: {hint}"}

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"Ошибка разбора JSON: {exc}"}

    jobs = data.get("jobs")
    if not jobs:
        return {"error": "fio не вернул данных о задачах"}

    job = jobs[0]
    mode = job.get("read" if "read" in test_id else "write", {})
    if not mode:
        return {"error": f"Отсутствуют данные для {test_id}"}

    iops = mode.get("iops", 0)
    bw_bytes = mode.get("bw_bytes", 0)
    lat = mode.get("lat_ns", {})
    lat_avg = lat.get("mean", 0) / 1e6
    lat_p99 = lat.get("percentile", {}).get("99.000000", 0) / 1e6
    bw_mb = bw_bytes / (1024 * 1024)

    return {"iops": iops, "bw_mb": bw_mb, "lat_avg": lat_avg, "lat_p99": lat_p99}


def run_precondition(disk_info, cancel_event=None):
    """Запускает прекондишнинг (полная запись) диска. Возвращает True при успехе."""
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
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                proc.wait()
                return False
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
        return proc.returncode == 0
    except Exception:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        return False


def optimize_nvme_args(test_id, args_list, pcie_info):
    """Оптимизирует аргументы fio для NVMe в зависимости от поколения PCIe."""
    if not pcie_info:
        return args_list

    gen = pcie_info.get("gen")
    if gen is None:
        return args_list

    def set_arg(args, key, value):
        new_args = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == key and i + 1 < len(args):
                new_args.append(key)
                new_args.append(str(value))
                i += 2
                continue
            if arg.startswith(f"{key}="):
                new_args.append(f"{key}={value}")
                i += 1
                continue
            new_args.append(arg)
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



def _status_style(status):
    """Форматирует статус теста тегами rich."""
    if status == "done":
        return "[bold green]done[/bold green]"
    return "[bold red]undone[/bold red]"


def build_results_table(disks, results):
    """Строит таблицу результатов с внешней таблицей и внутренними таблицами по дискам."""
    from rich.box import ROUNDED, SIMPLE

    outer = Table(
        show_header=True,
        box=ROUNDED,
    )
    outer.add_column("№", justify="right", style="bold", width=4)
    outer.add_column("Накопитель", min_width=30)
    outer.add_column("Результаты тестирования накопителей (FIO)", min_width=70)

    for idx, disk in enumerate(disks, 1):
        disk_name = disk["name"]
        model = disk.get("model", "N/A").strip()
        tran = disk.get("tran", "N/A")
        serial = disk.get("serial", "N/A").strip()
        slot = disk.get("slot", "N/A")
        size = disk.get("size", "N/A")

        disk_lines = [
            f"/dev/{disk_name}",
            model,
            tran,
            f"SN: {serial}",
            f"Slot: {slot}",
            f"Размер: {size}",
        ]
        disk_info_text = "\n".join(disk_lines)

        inner = Table(box=SIMPLE, show_header=True, show_edge=False)
        inner.add_column("Профиль теста", min_width=18)
        inner.add_column("Блок", justify="center")
        inner.add_column("IOPS", justify="right")
        inner.add_column("Скорость (МБ/с)", justify="right")
        inner.add_column("Lat Avg (мс)", justify="right")
        inner.add_column("Lat P99 (мс)", justify="right")
        inner.add_column("Статус", justify="center")

        disk_results = results[idx - 1] if idx - 1 < len(results) else {}
        test_order = ["seq_read", "seq_write", "rand_read", "rand_write"]
        for test_id in test_order:
            res = disk_results.get(test_id, {})
            test_name = TEST_NAMES.get(test_id, test_id)
            if "error" in res:
                inner.add_row(
                    test_name, "—", "—", "—", "—", "—", _status_style("undone"),
                )
            else:
                iops = f"{res.get('iops', 0):,.0f}"
                bw = f"{res.get('bw_mb', 0):.1f}"
                lat_avg = f"{res.get('lat_avg', 0):.2f}"
                lat_p99 = f"{res.get('lat_p99', 0):.2f}"
                status = res.get("status", "undone")
                inner.add_row(
                    test_name, res.get("bs", "4k"), iops, bw, lat_avg, lat_p99,
                    _status_style(status),
                )

        outer.add_row(str(idx), disk_info_text, inner)

    return outer


def process_task_result(results, idx, disk, t, fio_args, res):
    """Обрабатывает результат задачи и записывает его в общий словарь результатов."""
    bs = "4k"
    for a in fio_args:
        if a.startswith("--bs="):
            bs = a.split("=", 1)[1]
            break

    if "error" in res:
        results[idx][t] = {"error": res["error"], "status": "undone", "bs": bs}
        console.print(
            f"  [bold red]ОШИБКА[/bold red] {disk['name']}/{t}: {res['error']}"
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

    # Настройка пороговых значений с возможностью переопределения
    thresholds = {
        "nvme": dict(nvme.THRESHOLDS),
        "sas": dict(sas.THRESHOLDS),
        "sata": dict(sata.THRESHOLDS),
    }
    if args.threshold_nvme:
        thresholds["nvme"].update(parse_custom_thresholds(args.threshold_nvme))
    if args.threshold_sas:
        thresholds["sas"].update(parse_custom_thresholds(args.threshold_sas))
    if args.threshold_sata:
        thresholds["sata"].update(parse_custom_thresholds(args.threshold_sata))

    console.print("[bold]Сканирование дисков...[/bold]")

    try:
        system_disks, disks = scan_disks(INTERFACE_CONFIGS)
    except Exception as exc:
        console.print(f"[bold red]Ошибка сканирования:[/bold red] {exc}")
        sys.exit(1)

    if system_disks:
        console.print("\n[bold]Системные диски:[/bold]")
        for sd in system_disks:
            root_info = sd.get("root_partition", "")
            name = sd["name"]
            console.print(
                f"  [yellow]/dev/{name}[/yellow]"
                + (f" (root: {root_info})" if root_info else "")
            )

    console.print("\n[bold]Целевые диски:[/bold]")
    for i, d in enumerate(disks, 1):
        name = d["name"]
        model = d.get("model", "N/A").strip()
        console.print(f"  [green]{i}. /dev/{name}[/green] {model}")

    if not disks:
        console.print("[bold yellow]Целевые диски не найдены.[/bold yellow]")
        sys.exit(0)

    mode = "sequential" if args.sequential else "parallel"
    console.print(f"\n[bold]Режим: {mode}[/bold]")

    if args.confirm:
        target_names = ", ".join(f"/dev/{d['name']}" for d in disks)
        console.print(f"\n[bold yellow]Будут протестированы:[/bold yellow] {target_names}")
        try:
            answer = input("Начать тестирование? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[bold yellow]Отменено.[/bold yellow]")
            sys.exit(0)
        if answer not in ("y", "yes", "д", "да"):
            console.print("[bold yellow]Отменено пользователем.[/bold yellow]")
            sys.exit(0)

    cancel_event = threading.Event()

    # Прекондишнинг
    if args.precond:
        console.print("\n[bold]Прекондишнинг...[/bold]")
        for d in disks:
            name = d["name"]
            console.print(f"  Прекондишнинг /dev/{name}...")
            ok = run_precondition(d, cancel_event=cancel_event)
            if ok:
                console.print(f"  [green]Готово[/green] /dev/{name}")
            else:
                console.print(f"  [red]Ошибка[/red] /dev/{name}")

    if args.runtime != 30:
        console.print(f"\n[bold]Длительность теста: {args.runtime} сек[/bold]")

    # Подготовка задач
    results = [{} for _ in disks]
    tasks = []

    for disk_idx, disk in enumerate(disks):
        tran = disk.get("tran", "").lower()
        if "nvme" in tran:
            tran_key = "nvme"
        elif "sas" in tran:
            tran_key = "sas"
        else:
            tran_key = "sata"

        tests = INTERFACE_CONFIGS.get(tran_key, INTERFACE_CONFIGS["sata"])
        pcie_info = disk.get("pcie_info") if "nvme" in tran else None

        for test in tests:
            t = test["id"]
            fio_args = list(test["args"])

            # Переопределение длительности теста
            fio_args = [
                f"--runtime={args.runtime}" if a.startswith("--runtime=") else a
                for a in fio_args
            ]

            # Оптимизация для NVMe
            if "nvme" in tran and pcie_info:
                fio_args = optimize_nvme_args(t, fio_args, pcie_info)

            results[disk_idx]["_thresholds"] = thresholds.get(tran_key, {})
            tasks.append((disk_idx, disk, t, fio_args))

    # Запуск тестов
    if args.sequential:
        try:
            for disk_idx, disk, t, fio_args in tasks:
                res = run_fio_test(disk, t, fio_args, cancel_event=cancel_event)
                process_task_result(results, disk_idx, disk, t, fio_args, res)
        except KeyboardInterrupt:
            cancel_event.set()
            console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
            sys.exit(130)
    else:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            future_map = {}
            for disk_idx, disk, t, fio_args in tasks:
                fut = pool.submit(run_fio_test, disk, t, fio_args, cancel_event=cancel_event)
                future_map[fut] = (disk_idx, disk, t, fio_args)

            for fut in concurrent.futures.as_completed(future_map):
                disk_idx, disk, t, fio_args = future_map[fut]
                res = fut.result()
                process_task_result(results, disk_idx, disk, t, fio_args, res)
        except KeyboardInterrupt:
            cancel_event.set()
            console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
            sys.exit(130)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Вывод результатов
    table = build_results_table(disks, results)
    console.print()
    console.print(table)

    generate_report(disks, results, output_path=args.output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"\n[bold red]Фатальная ошибка:[/bold red] {exc}")
        sys.exit(1)
