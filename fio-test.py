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
    python fio-test.py -t           — тестовый режим (пробные данные без fio)
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

from configs import nvme, sas, sata
from utils.diagnostics import DiagnosticSampler, collect_static_info
from utils.reporter import generate_report
from utils.scanner import scan_disks
from utils.table_renderer import build_results_table

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


_SHORT_FLAGS = {"c", "s", "p", "t", "d"}


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
            "  python fio-test.py -t                        — тестовый режим (пробные данные)\n"
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
        "-d", "--diagnostic", action="store_true",
        help="Диагностический режим: сэмплинг линка/температуры/нагрузки "
             "в пер-секундные таблицы отчёта",
    )
    parser.add_argument(
        "-t", "--test", action="store_true",
        help="Тестовый режим: заполнить таблицу пробными значениями без запуска fio",
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


def build_fake_disks() -> list:
    """Фейковые диски (5 шт., разные интерфейсы) для проверки вёрстки таблицы."""
    return [
        {
            "name": "nvme0n1", "path": "/dev/nvme0n1",
            "model": "SAMSUNG MZWLO1T9HCJR-00A07", "serial": "S795NC0Y101175",
            "tran": "NVME", "size": "1.7T", "phy_sec": 512, "slot": "nvme0",
            "pcie_info": {"gen": 5, "width": 4, "speed_gts": 32.0}, "root_partition": None,
        },
        {
            "name": "nvme1n1", "path": "/dev/nvme1n1",
            "model": "SAMSUNG MZWLO1T9HCJR-00A07", "serial": "S795NC0Y101184",
            "tran": "NVME", "size": "1.7T", "phy_sec": 512, "slot": "nvme1",
            "pcie_info": {"gen": 4, "width": 4, "speed_gts": 16.0}, "root_partition": None,
        },
        {
            "name": "sda", "path": "/dev/sda",
            "model": "SEAGATE ST1800MM0129", "serial": "ABC12345",
            "tran": "SAS", "size": "1.8T", "phy_sec": 512, "slot": "0:2:0:0",
            "pcie_info": {"gen": None, "width": None, "speed_gts": None}, "root_partition": None,
        },
        {
            "name": "sdb", "path": "/dev/sdb",
            "model": "SAMSUNG PM883 960GB", "serial": "S3Z7NB0T0000001",
            "tran": "SATA", "size": "960G", "phy_sec": 512, "slot": "1:0:0:0",
            "pcie_info": {"gen": None, "width": None, "speed_gts": None}, "root_partition": None,
        },
        {
            "name": "sdc", "path": "/dev/sdc",
            "model": "WDC WD4004FZWX", "serial": "WD-WCC4E0TST01",
            "tran": "SATA", "size": "4T", "phy_sec": 4096, "slot": "1:0:0:1",
            "pcie_info": {"gen": None, "width": None, "speed_gts": None}, "root_partition": None,
        },
    ]


def build_fake_results(disks: list) -> list:
    """Пробные результаты: во всех ячейках значение 'test'."""
    fake = {"bs": "test", "iops": "test", "bw_mb": "test",
            "lat_avg": "test", "lat_p99": "test", "status": "test"}
    return [{test_id: dict(fake) for test_id in TEST_NAMES} for _ in disks]


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


def _kill_process_group(proc):
    """Отправляет SIGTERM всей группе процессов."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        pass


def _run_io_process(cmd, cancel_event):
    """Запускает процесс и ждёт завершения.

    Возвращает (proc, stdout, stderr) либо None при отмене или ошибке запуска.
    FileNotFoundError пробрасывается наверх для точной диагностики.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                _kill_process_group(proc)
                proc.wait()
                return None
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
        stdout, stderr = proc.communicate()
        return proc, stdout, stderr
    except FileNotFoundError:
        raise
    except Exception:
        if proc and proc.poll() is None:
            _kill_process_group(proc)
        return None


def _max_iodepth(iodepth_level: dict):
    """Возвращает максимальную достигнутую глубину очереди из гистограммы fio."""
    best = None
    for depth, count in iodepth_level.items():
        if count:
            best = depth
    return int(best) if best is not None else None


def _parse_fio_result(test_id, stdout):
    """Разбирает stdout fio (JSON) в результат + диагностические метрики."""
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

    res = {"iops": iops, "bw_mb": bw_mb, "lat_avg": lat_avg, "lat_p99": lat_p99}

    # Диагностика из fio-JSON (заполняется при --diagnostic и без него)
    usage = job.get("usage") or {}
    res["cpu_user"] = round(usage.get("user", 0), 1)
    res["cpu_sys"] = round(usage.get("sys", 0), 1)

    lat_perc = lat.get("percentile") or {}
    for key, pkey in (("p50", "50.000000"), ("p90", "90.000000"),
                      ("p99", "99.000000"), ("p99_9", "99.900000")):
        val = lat_perc.get(pkey)
        if val:
            res[f"clat_{key}_ms"] = round(val / 1e6, 3)

    slat = mode.get("slat_ns") or {}
    if slat.get("mean"):
        res["slat_avg_ms"] = round(slat["mean"] / 1e6, 4)

    res["io_kb"] = int(mode.get("io_kbytes", 0))
    res["iodepth"] = _max_iodepth(job.get("iodepth_level") or {})

    return res


def run_fio_test(disk_info, test_id, base_args, cancel_event=None, diag_store=None):
    """Запускает fio-тест. Поддерживает отмену через cancel_event.

    В диагностическом режиме (diag_store) параллельно сэмплирует линк,
    температуру и реальную нагрузку на диск; сэмплы и сводка сохраняются
    в памяти и попадают в единый файл отчёта.
    """
    disk_path = disk_info["path"]
    cmd = [
        "fio",
        "--name", test_id,
        "--filename", disk_path,
        "--direct=1",
        "--ioengine=libaio",
        "--group_reporting",
        "--norandommap",
        "--output-format=json",
    ] + list(base_args)

    stop_event = threading.Event()
    sampler = None
    sampler_thread = None

    if diag_store is not None:
        sampler = DiagnosticSampler(disk_info)
        sampler_thread = threading.Thread(
            target=sampler.run, args=(stop_event,), daemon=True
        )
        sampler_thread.start()

    res = None
    try:
        try:
            result = _run_io_process(cmd, cancel_event)
        except FileNotFoundError:
            res = {"error": "Утилита fio не найдена. Установите fio и повторите попытку."}
        else:
            if result is None:
                if cancel_event and cancel_event.is_set():
                    res = {"error": "Тест отменён пользователем"}
                else:
                    res = {"error": "Ошибка запуска fio"}
            else:
                proc, stdout, stderr = result
                stdout = stdout.decode() if stdout else ""
                stderr = stderr.decode() if stderr else ""
                if proc.returncode != 0:
                    hint = stderr.strip()[:200] if stderr else ""
                    res = {"error": f"fio завершился с кодом {proc.returncode}: {hint}"}
                else:
                    res = _parse_fio_result(test_id, stdout)
    finally:
        stop_event.set()
        if sampler_thread:
            sampler_thread.join(timeout=5)

    if res is not None:
        if sampler:
            summary = sampler.summary()
            res["diag"] = summary
            diag_store.setdefault(disk_info["name"], {})[test_id] = {
                "samples": sampler.samples,
                "summary": summary,
            }

    return res


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
    try:
        result = _run_io_process(cmd, cancel_event)
    except FileNotFoundError:
        return False
    if result is None:
        return False
    proc, _, _ = result
    return proc.returncode == 0


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


def run_disk_tests(disk_idx, disk, plan, results, cancel_event=None, diag_store=None):
    """Запускает все тесты одного диска строго последовательно.

    Параллелизация идёт по дискам, а не по отдельным тестам: несколько fio,
    конкурирующих за один накопитель, делят его шину и занижают результаты.
    """
    for t, fio_args in plan:
        res = run_fio_test(disk, t, fio_args, cancel_event=cancel_event, diag_store=diag_store)
        process_task_result(results, disk_idx, disk, t, fio_args, res)


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

    if args.test:
        disks = build_fake_disks()
        results = build_fake_results(disks)
        console.print("[bold]Тестовый режим: пробные данные, fio не запускается[/bold]")
        console.print()
        table = build_results_table(disks, results, TEST_NAMES)
        console.print(table)
        report_path = generate_report(disks, results, TEST_NAMES, output_path=args.output)
        console.print(f"[bold green]Отчёт сохранён: {report_path}[/bold green]")
        return

    console.print("[bold]Сканирование дисков...[/bold]")

    try:
        system_disks, disks = scan_disks(INTERFACE_CONFIGS)
    except Exception as exc:
        console.print(f"[bold red]Ошибка сканирования:[/bold red] {exc}")
        sys.exit(1)

    if args.diagnostic:
        for d in disks:
            d["diag_static"] = collect_static_info(d)

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

    # Сбор диагностических сэмплов в памяти: {диск: {тест: {"samples", "summary"}}}
    diag_store = {}
    if args.diagnostic:
        console.print("\n[bold]Диагностика: пер-секундные сэмплы линка/"
                      "температуры/нагрузки — в единый файл отчёта[/bold]")

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

    # Подготовка задач: по одному плану тестов на диск (тесты диска — подряд)
    results = [{} for _ in disks]
    disk_plans = []

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
        results[disk_idx]["_thresholds"] = thresholds.get(tran_key, {})

        plan = []
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

            plan.append((t, fio_args))

        disk_plans.append((disk_idx, disk, plan))

    # Запуск тестов
    if args.sequential:
        try:
            for disk_idx, disk, plan in disk_plans:
                run_disk_tests(disk_idx, disk, plan, results, cancel_event, diag_store)
        except KeyboardInterrupt:
            cancel_event.set()
            console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
            sys.exit(130)
    else:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            future_map = {}
            for disk_idx, disk, plan in disk_plans:
                fut = pool.submit(
                    run_disk_tests, disk_idx, disk, plan, results,
                    cancel_event=cancel_event, diag_store=diag_store,
                )
                future_map[fut] = disk_idx

            for fut in concurrent.futures.as_completed(future_map):
                fut.result()
        except KeyboardInterrupt:
            cancel_event.set()
            console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
            sys.exit(130)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Вывод результатов
    table = build_results_table(disks, results, TEST_NAMES)
    console.print()
    console.print(table)

    report_path = generate_report(
        disks, results, TEST_NAMES, output_path=args.output, diag_store=diag_store
    )
    console.print(f"[bold green]Отчёт сохранён: {report_path}[/bold green]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Прервано пользователем.[/bold yellow]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"\n[bold red]Фатальная ошибка:[/bold red] {exc}")
        sys.exit(1)
