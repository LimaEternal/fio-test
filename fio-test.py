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
    python fio-test.py -c -p        — с подтверждением и предварительным заполнением
    python fio-test.py -r 60            — 60 сек на тест
    python fio-test.py -l               — подробное логирование: отчёт обновляется
                                          по мере завершения тестов (мониторинг в отчёте)
    python fio-test.py -t               — тестовый режим (пробные данные без fio)
    python fio-test.py -a 1 3-5         — протестировать только диски 1 и 3..5
                                          (номера и диапазоны из нумерованного списка)
    python fio-test.py -d 4 6-8         — протестировать все несистемные диски,
                                          кроме 4 и 6..8
    python fio-test.py -o my.md         — свой путь отчёта
    python fio-test.py -t               — тестовый режим (пробные данные без fio)
"""

import argparse
import concurrent.futures
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

from rich.console import Console

from utils.diagnostics import DiagnosticSampler, collect_static_info
from utils.format import format_duration
from utils.fio_config import parse_fio_jobfile
from utils.prefill import disk_has_data, prefill_disks, summarize_data_state
from utils.process import kill_process_tree, run_process
from utils.reporter import generate_report
from utils.scanner import scan_disks
from utils.table_renderer import build_results_table
from utils.tuner import SystemTuner

console = Console(color_system=None, highlight=False)

CONFIG_DIR = Path(__file__).resolve().parent / "configs"

INTERFACES = ["nvme", "sas", "sata"]

# Отображаемые имена тестов для отчёта: id секции .fio -> заголовок
FRIENDLY_TEST_NAMES = {
    "seq_read": "Послед. чтение",
    "seq_write": "Послед. запись",
    "rand_read": "Случ. чтение 4k",
    "rand_write": "Случ. запись 4k",
}


def _load_fio_configs():
    """Читает configs/<interface>.fio -> {интерфейс: {id: [аргументы]}}."""
    return {
        iface: parse_fio_jobfile(CONFIG_DIR / f"{iface}.fio")
        for iface in INTERFACES
    }


def _load_thresholds():
    """Читает configs/thresholds.json -> {интерфейс: {id: {порог: значение}}}."""
    path = CONFIG_DIR / "thresholds.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# Конфигурации интерфейсов и пороги (загружаются при старте, fail-fast)
INTERFACE_CONFIGS = _load_fio_configs()
INTERFACE_THRESHOLDS = _load_thresholds()

TEST_NAMES = {}
for _tests in INTERFACE_CONFIGS.values():
    for _tid in _tests:
        TEST_NAMES[_tid] = FRIENDLY_TEST_NAMES.get(_tid, _tid)

# Отношение p99 к среднему, выше которого перцентиль считается мусором.
# На fio-3.28 c fixedbufs/sqthread_poll встречался clat p99 ~17 с при
# avg < 10 мс (недостоверная гистограмма) — такие значения не выводим.
P99_RELIABILITY_FACTOR = 500


_SHORT_FLAGS = {"c", "s", "p", "t", "l", "n"}


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
            "  python fio-test.py -c -p                     — с подтверждением и предварительным заполнением\n"
            "  python fio-test.py -r 60                     — 60 секунд на каждый тест\n"
            "  python fio-test.py -l                        — подробное логирование (мониторинг в отчёте)\n"
            "  python fio-test.py -t                        — тестовый режим (пробные данные)\n"
            "  python fio-test.py -a 1 3-5                  — только диски 1 и 3..5 (номера/диапазоны)\n"
            "  python fio-test.py -d 4 6-8                  — все диски, кроме 4 и 6..8\n"
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
        "-p", "--prefill", action="store_true",
        help="Предварительное заполнение перед тестами",
    )
    parser.add_argument(
        "-l", "--logging", action="store_true",
        help="Подробное логирование: посекундный мониторинг линка/температуры/"
             "нагрузки в MD-отчёт (+ колонка Lat P99)",
    )
    parser.add_argument(
        "-n", "--no-tune", action="store_true",
        help="Отключить автоматическую настройку системы для производительности",
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
        "-r", "--runtime", type=int, default=None,
        help="Длительность каждого теста в секундах (по умолчанию — из конфига .fio)",
    )
    def disk_token_type(token: str):
        try:
            return _expand_token(token)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"неверный формат номера/диапазона: '{token}'"
            )

    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "-a", "--add", nargs="*", type=disk_token_type, default=None, metavar="N",
        help="Протестировать только указанные диски (номера или диапазоны из "
             "пронумерованного списка, через пробел: 1 3-5). Без номеров — "
             "запросит список интерактивно",
    )
    selection.add_argument(
        "-d", "--delete", nargs="*", type=disk_token_type, default=None, metavar="N",
        help="Протестировать все несистемные диски, кроме указанных (номера или "
             "диапазоны, через пробел: 1 3-5). Без номеров — запросит список "
             "интерактивно",
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
    args = parser.parse_args(_expand_short_flags(sys.argv[1:]))
    if args.add is not None:
        args.add = [n for tok in args.add for n in tok]
    if args.delete is not None:
        args.delete = [n for tok in args.delete for n in tok]
    return args


def check_threshold(test_id, res, thresholds):
    """Проверяет результат теста по пороговым значениям. Возвращает 'PASS' или 'FAIL'."""
    thr = thresholds.get(test_id)
    if thr is None or "error" in res:
        return "FAIL"
    if "min_bw_mb" in thr:
        if res.get("bw_mb", 0) >= thr["min_bw_mb"]:
            return "PASS"
    elif "min_iops" in thr:
        if res.get("iops", 0) >= thr["min_iops"]:
            return "PASS"
    return "FAIL"


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


def _expand_token(token: str) -> list:
    """Разворачивает один токен номера/диапазона в список целых.

    '5'   → [5]
    '1-3' → [1, 2, 3]
    '3-1' → [3, 2, 1]
    Неверный формат → ValueError.
    """
    token = token.strip()
    if "-" in token:
        start_s, end_s = token.split("-", 1)
        start = int(start_s)
        end = int(end_s)
        if start <= end:
            return list(range(start, end + 1))
        return list(range(start, end - 1, -1))
    return [int(token)]


def parse_disk_numbers(raw: str) -> list:
    """Разбирает строку номеров и диапазонов '1-3 5' в список целых (пустая строка → [])."""
    result = []
    for token in raw.strip().split():
        result.extend(_expand_token(token))
    return result


def _input_disk_numbers(prompt: str) -> list:
    """Спрашивает у пользователя номера дисков (через пробел или диапазоном) и возвращает их список.

    При неверном формате запрашивает ввод повторно; EOF/Ctrl-C → «Отменено».
    """
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[bold yellow]Отменено.[/bold yellow]")
            sys.exit(0)
        try:
            return parse_disk_numbers(raw)
        except ValueError:
            console.print(
                "[bold red]ОШИБКА:[/bold red] неверный формат. Пример: '1 3-5'"
            )


def apply_disk_selection(disks: list, args) -> list:
    """Применяет выбор дисков --add/--delete к пронумерованному списку (1..N).

    --add 1 2 3    — оставить только диски 1, 2, 3; --add без номеров → [].
    --delete 4 5 6 — исключить диски 4, 5, 6 из полного набора;
                     --delete без номеров → все диски.
    Флаг не задан (None) → список без изменений.
    Неверные номера (вне диапазона) считаются ошибкой и останавливают запуск.
    """
    if args.add is None and args.delete is None:
        return disks

    numbers = args.add if args.add is not None else args.delete

    valid = set(range(1, len(disks) + 1))
    invalid = sorted(set(numbers) - valid)
    if invalid:
        console.print(
            f"[bold red]ОШИБКА:[/bold red] неверные номера дисков: "
            f"{', '.join(str(n) for n in invalid)} "
            f"(доступны номера 1..{len(disks)})"
        )
        sys.exit(1)

    selected = set(numbers)
    if args.add is not None:
        return [d for i, d in enumerate(disks, 1) if i in selected]
    return [d for i, d in enumerate(disks, 1) if i not in selected]


def _build_run_info(args, prefill_duration=None, tests_duration=None,
                    data_state=None) -> dict:
    """Собирает мета-информацию о запуске для секции «Параметры запуска» отчёта."""
    mode = "последовательный" if args.sequential else "параллельный"
    flags = [
        ("Режим", mode),
        ("Предварительное заполнение", "включено" if args.prefill else "выключено"),
        ("Подробные логи", "включены" if args.logging else "выключены"),
        ("Автонастройка системы", "выключена" if args.no_tune else "включена"),
        (
            "Длительность теста",
            f"{args.runtime} сек" if args.runtime is not None else "по конфигу (.fio)",
        ),
    ]
    if data_state is not None:
        flags.append(("Данные на дисках", data_state))
    if prefill_duration is not None:
        flags.append(("Время предзаполнения", format_duration(prefill_duration)))
    if tests_duration is not None:
        flags.append(("Время тестов", format_duration(tests_duration)))
    if args.add is not None:
        flags.append(("Выбор дисков (--add)", ", ".join(str(n) for n in args.add)))
    if args.delete is not None:
        flags.append(("Выбор дисков (--delete)", ", ".join(str(n) for n in args.delete)))
    if args.threshold_nvme:
        flags.append(("Пороги NVMe", args.threshold_nvme))
    if args.threshold_sas:
        flags.append(("Пороги SAS", args.threshold_sas))
    if args.threshold_sata:
        flags.append(("Пороги SATA", args.threshold_sata))
    if args.output:
        flags.append(("Выходной отчёт", args.output))
    command = "python fio-test.py " + " ".join(sys.argv[1:])
    return {"command": command.strip(), "flags": flags}


def _collect_fio_configs(disks: list) -> dict:
    """Возвращает {интерфейс: сырое содержимое .fio} для интерфейсов целевых дисков."""
    used = set()
    for disk in disks:
        tran = disk.get("tran", "").lower()
        if "nvme" in tran:
            used.add("nvme")
        elif "sas" in tran:
            used.add("sas")
        else:
            used.add("sata")

    configs = {}
    for name in INTERFACES:
        if name in used:
            path = CONFIG_DIR / f"{name}.fio"
            try:
                configs[name] = path.read_text(encoding="utf-8")
            except OSError:
                configs[name] = f"# Файл {path.name} не найден"
    return configs


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
    """Валидирует .fio-конфиги и пороги всех интерфейсов."""
    valid_bs = {"4k", "8k", "16k", "32k", "64k", "128k", "256k", "512k", "1m"}
    required_thresh_keys = {"seq_read", "seq_write", "rand_read", "rand_write"}

    for name, tests in INTERFACE_CONFIGS.items():
        desc = name

        # Проверка секций .fio
        if not tests:
            console.print(
                f"[red]ОШИБКА:[/red] {desc}: {name}.fio не содержит секций"
            )
            sys.exit(1)
        for tid, args in tests.items():
            for a in args:
                if a.startswith("--bs="):
                    bs_val = a.split("=", 1)[1].lower()
                    if bs_val not in valid_bs:
                        console.print(
                            f"[red]ОШИБКА:[/red] {desc}: недопустимый bs={bs_val} "
                            f"в тесте {tid}"
                        )
                        sys.exit(1)
            if not any(a.startswith("--ioengine=") for a in args):
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: тест '{tid}' — "
                    f"не указан ioengine (движок должен быть в конфиге)"
                )
                sys.exit(1)
            if "--direct=1" not in args:
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: тест '{tid}' — "
                    f"нет direct=1 (обход page cache обязателен)"
                )
                sys.exit(1)
            if any(a.startswith("--fsync=") for a in args):
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: тест '{tid}' — "
                    f"fsync искажает замер скорости, уберите его"
                )
                sys.exit(1)
            if "--output-format=json" not in args:
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: тест '{tid}' — "
                    f"нет output-format=json (скрипт парсит JSON)"
                )
                sys.exit(1)

        # Проверка порогов
        thresh = INTERFACE_THRESHOLDS.get(name)
        if not isinstance(thresh, dict):
            console.print(
                f"[red]ОШИБКА:[/red] {desc}: пороги не являются словарём"
            )
            sys.exit(1)
        missing_t = required_thresh_keys - set(thresh.keys())
        if missing_t:
            console.print(
                f"[red]ОШИБКА:[/red] {desc}: пороги не содержат ключей {missing_t}"
            )
            sys.exit(1)
        for tid, tv in thresh.items():
            if not isinstance(tv, dict):
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: пороги['{tid}'] — не словарь"
                )
                sys.exit(1)
            if "min_bw_mb" not in tv and "min_iops" not in tv:
                console.print(
                    f"[red]ОШИБКА:[/red] {desc}: пороги['{tid}'] — "
                    f"нет min_bw_mb или min_iops"
                )
                sys.exit(1)


def _run_io_process(cmd, cancel_event):
    """Запускает процесс и ждёт завершения (обёртка над utils.process.run_process).

    Возвращает (proc, stdout, stderr) либо None при отмене или ошибке запуска.
    FileNotFoundError пробрасывается наверх для точной диагностики.
    """
    return run_process(cmd, cancel_event)


def _save_raw_fio_output(disk_name: str, test_id: str, stdout: str) -> None:
    """Сохраняет сырой stdout fio в reports/raw/ для диагностики метрик.

    Нужно для расследования аномальных значений (например, недостоверных
    перцентилей clat): по сохранённому JSON можно увидеть, что именно отдал
    fio, не перезапуская тесты.
    """
    try:
        raw_dir = Path("reports") / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"fio-{disk_name}-{test_id}-{int(time.time())}.json"
        path.write_text(stdout, encoding="utf-8")
    except OSError:
        pass


def _max_iodepth(iodepth_level: dict):
    """Возвращает максимальную достигнутую глубину очереди из гистограммы fio.

    fio помечает верхнюю (переполненную) корзину гистограммы строкой вида
    ">=64" — такие ключи разбираются по цифрам.
    """
    best = None
    for depth, count in iodepth_level.items():
        if count:
            digits = "".join(ch for ch in str(depth) if ch.isdigit())
            if digits:
                best = int(digits)
    return best


def _percentile_value(percentile: dict, target: float):
    """Возвращает значение перцентиля из fio-JSON или None.

    Ключи обычно форматируются как "99.000000", но у старых версий fio могут
    отличаться — сначала ищем точный строковый ключ, затем числовое совпадение.
    """
    if not percentile:
        return None
    exact = percentile.get(f"{target:.6f}")
    if exact is not None:
        return exact
    target_round = round(target, 2)
    for key, value in percentile.items():
        try:
            if round(float(key), 2) == target_round:
                return value
        except (TypeError, ValueError):
            continue
    return None


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
    lat = mode.get("lat_ns") or {}
    lat_avg = lat.get("mean", 0) / 1e6
    bw_mb = bw_bytes / 1_000_000

    # Перцентили по умолчанию fio пишет в clat_ns (clat_percentiles=1,
    # lat_percentiles=0); при lat_percentiles=1 — в lat_ns. Пробуем оба.
    perc = (mode.get("clat_ns") or {}).get("percentile")
    if not perc:
        perc = lat.get("percentile")
    p99 = _percentile_value(perc or {}, 99.0) or 0
    lat_p99 = p99 / 1e6

    res = {"iops": iops, "bw_mb": bw_mb, "lat_avg": lat_avg, "lat_p99": lat_p99}

    # Загрузка CPU: современный fio пишет usr_cpu/sys_cpu на уровне job,
    # старые версии — usage.usr/usage.sys (в тестах встречается usage.user).
    usage = job.get("usage") or {}
    cpu_user = job.get("usr_cpu")
    if cpu_user is None:
        cpu_user = usage.get("usr")
    if cpu_user is None:
        cpu_user = usage.get("user")
    cpu_sys = job.get("sys_cpu")
    if cpu_sys is None:
        cpu_sys = usage.get("sys")
    res["cpu_user"] = round(float(cpu_user or 0), 1)
    res["cpu_sys"] = round(float(cpu_sys or 0), 1)

    for key, target in (("p50", 50.0), ("p90", 90.0),
                        ("p99", 99.0), ("p99_9", 99.9)):
        val = _percentile_value(perc or {}, target)
        if val:
            res[f"clat_{key}_ms"] = round(val / 1e6, 3)

    # Контроль достоверности перцентилей: p99, в сотни раз превышающий
    # среднее, физически невозможен для стабильного теста. Такие значения
    # помечаются, чтобы отчёт показал "—" вместо вводящих в заблуждение цифр.
    res["lat_p99_unreliable"] = bool(
        lat_p99 and lat_avg and lat_p99 > lat_avg * P99_RELIABILITY_FACTOR
    )

    slat = mode.get("slat_ns") or {}
    if slat.get("mean"):
        res["slat_avg_ms"] = round(slat["mean"] / 1e6, 4)

    res["io_kb"] = int(mode.get("io_kbytes", 0))
    res["iodepth"] = _max_iodepth(job.get("iodepth_level") or {})
    res["elapsed_s"] = round(float(job.get("elapsed") or 0), 1)

    return res


def run_fio_test(disk_info, test_id, base_args, cancel_event=None, diag_store=None, tuner=None,
                 state_lock=None, live_store=None):
    """Запускает fio-тест. Поддерживает отмену через cancel_event.

    В диагностическом режиме (diag_store) параллельно сэмплирует линк,
    температуру и реальную нагрузку на диск; сэмплы и сводка сохраняются
    в памяти и попадают в единый файл отчёта.

    state_lock защищает запись в общие diag_store/live_store от гонок
    с фоновым writer-потоком инкрементального отчёта.
    """
    disk_path = disk_info["path"]
    # Движок/direct/output-format приходят из .fio-конфига (секция [global]),
    # здесь добавляются только инфраструктурные параметры запуска.
    cmd = [
        "fio",
        "--name", test_id,
        "--filename", disk_path,
    ] + list(base_args)

    if tuner:
        numa_cpus = tuner.get_numa_cpus(disk_info["name"])
        if numa_cpus:
            cmd.extend(["--cpus_allowed", numa_cpus])

    # В диагностическом режиме просим fio писать посекундные логи нагрузки
    # (write_bw_log/write_iops_log): скорость/IOPS по секундам берутся из них,
    # по таймстампу вливаются в сэмплы после завершения теста.
    fio_log_prefix = None
    if diag_store is not None:
        log_dir = Path("reports")
        log_dir.mkdir(exist_ok=True)
        fio_log_prefix = str(log_dir / f"fio-{disk_info['name']}-{test_id}")
        cmd.extend([
            "--write_bw_log", fio_log_prefix,
            "--write_iops_log", fio_log_prefix,
            "--log_avg_msec", "1000",
            "--log_unix_epoch", "1",
            "--per_job_logs", "1",
        ])

    stop_event = threading.Event()
    sampler = None
    sampler_thread = None

    if diag_store is not None:
        sampler = DiagnosticSampler(disk_info)
        sampler_thread = threading.Thread(
            target=sampler.run, args=(stop_event,), daemon=True
        )
        sampler_thread.start()
        if live_store is not None:
            live_entry = {"test_id": test_id, "samples": sampler.samples}
            if state_lock is not None:
                with state_lock:
                    live_store[disk_info["name"]] = live_entry
            else:
                live_store[disk_info["name"]] = live_entry

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
                    res = {"error": "Ошибка запуска fio (проверьте права доступа к устройству)"}
            else:
                proc, stdout, stderr = result
                stdout = stdout.decode() if stdout else ""
                stderr = stderr.decode() if stderr else ""
                # Сырой JSON сохраняем в диагностическом режиме (-l): по нему
                # можно разобрать аномальные метрики без повторного прогона.
                if diag_store is not None and stdout.strip():
                    _save_raw_fio_output(disk_info["name"], test_id, stdout)
                if proc.returncode != 0:
                    hint = stderr.strip()[:500] if stderr else "нет вывода stderr"
                    res = {"error": f"fio завершился с кодом {proc.returncode}: {hint}"}
                else:
                    try:
                        res = _parse_fio_result(test_id, stdout)
                    except Exception as exc:
                        res = {"error": f"Ошибка разбора результата fio: {exc}"}
    finally:
        stop_event.set()
        if sampler_thread:
            sampler_thread.join(timeout=5)
        if live_store is not None:
            if state_lock is not None:
                with state_lock:
                    live_store.pop(disk_info["name"], None)
            else:
                live_store.pop(disk_info["name"], None)

    if res is not None:
        if sampler:
            if fio_log_prefix:
                merged = sampler.merge_fio_logs(fio_log_prefix)
            else:
                merged = False
            summary = sampler.summary()
            res["diag"] = summary
            sources = summary.get("sources") or {}
            notes = []
            if not sources.get("link"):
                notes.append("линк PCIe не удалось прочитать")
            if not sources.get("temp"):
                notes.append("температура недоступна: установите nvme-cli (нужен nvme smart-log)")
            summary["notes"] = notes
            if notes:
                console.print(
                    f"[yellow]Мониторинг /dev/{disk_info['name']} ({test_id}): "
                    f"{'; '.join(notes)}[/yellow]"
                )
            entry = {"samples": sampler.samples, "summary": summary}
            if state_lock is not None:
                with state_lock:
                    diag_store.setdefault(disk_info["name"], {})[test_id] = entry
            else:
                diag_store.setdefault(disk_info["name"], {})[test_id] = entry

    return res


# Переопределения аргументов fio по поколению PCIe: что нужно насытить, чтобы
# раскрыть линк. Единая таблица для Gen4/Gen5 — у Gen5 выше ёмкость линка,
# поэтому глубже очереди и крупнее блок для seq_read; seq_write одинаков
# (упирается в возможности контроллера, а не в линк).
_NVME_OVERRIDES = {
    4: {
        "seq_read": {"--numjobs": 2, "--iodepth": 16},
        "seq_write": {"--numjobs": 2, "--iodepth": 16},
        "rand_read": {"--numjobs": 8, "--iodepth": 32},
        "rand_write": {"--numjobs": 8, "--iodepth": 32},
    },
    5: {
        "seq_read": {"--numjobs": 4, "--iodepth": 16, "--bs": "256k"},
        "seq_write": {"--numjobs": 2, "--iodepth": 16},
        "rand_read": {"--numjobs": 16, "--iodepth": 16},
        "rand_write": {"--numjobs": 8, "--iodepth": 16},
    },
}


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
    for key, value in (_NVME_OVERRIDES.get(gen, {}).get(test_id) or {}).items():
        new_args = set_arg(new_args, key, value)
    return new_args


def process_task_result(results, idx, disk, t, fio_args, res, state_lock=None):
    """Обрабатывает результат задачи и записывает его в общий словарь результатов."""
    bs = "4k"
    for a in fio_args:

        if a.startswith("--bs="):
            bs = a.split("=", 1)[1]
            break

    if "error" in res:
        error_entry = {"error": res["error"], "status": "FAIL", "bs": bs}
        if state_lock is not None:
            with state_lock:
                results[idx][t] = error_entry
        else:
            results[idx][t] = error_entry
        console.print(
            f"  [bold red]ОШИБКА[/bold red] {disk['name']}/{t}: {res['error']}"
        )
        return

    if state_lock is not None:
        with state_lock:
            thresholds = results[idx].get("_thresholds", {})
    else:
        thresholds = results[idx].get("_thresholds", {})
    status = check_threshold(t, res, thresholds)
    res["status"] = status
    res["bs"] = bs
    if state_lock is not None:
        with state_lock:
            results[idx][t] = res
    else:
        results[idx][t] = res


def run_disk_tests(disk_idx, disk, plan, results, cancel_event=None, diag_store=None, tuner=None,
                   state_lock=None, report_queue=None, live_store=None):
    """Запускает все тесты одного диска строго последовательно.

    Параллелизация идёт по дискам, а не по отдельным тестам: несколько fio,
    конкурирующих за один накопитель, делят его шину и занижают результаты.

    После каждого завершённого теста в report_queue кладётся маркер — фоновый
    writer-поток перегенерирует MD-отчёт по мере поступления данных.
    В results[disk_idx] сохраняется длительность всех тестов диска (_wall_s).
    """
    start = time.monotonic()
    for t, fio_args in plan:
        res = run_fio_test(
            disk, t, fio_args, cancel_event=cancel_event, diag_store=diag_store,
            tuner=tuner, state_lock=state_lock, live_store=live_store,
        )
        process_task_result(results, disk_idx, disk, t, fio_args, res, state_lock=state_lock)
        if report_queue is not None:
            report_queue.put(disk_idx)
    duration = time.monotonic() - start
    if state_lock is not None:
        with state_lock:
            results[disk_idx]["_wall_s"] = duration
    else:
        results[disk_idx]["_wall_s"] = duration
    console.print(
        f"  Диск /dev/{disk['name']}: тесты заняли {format_duration(duration)}"
    )


REPORT_TICK = 2  # секунды между записями живых сэмплов в отчёт

_STOP = object()  # маркер остановки фонового writer-потока


def _default_report_path() -> Path:
    """Формирует путь отчёта по умолчанию (reports/fio_report_<timestamp>.md)."""
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return reports_dir / f"fio_report_{timestamp}.md"


def _snapshot_state(results, diag_store, live_store, state_lock):
    """Копирует текущее состояние для рендера отчёта (безопасно к воркерам).

    Живые сэмплы идущих тестов (live_store) вливаются в диагностическую копию,
    чтобы посекундные таблицы текущего теста попадали в отчёт.
    """
    with state_lock:
        results_snap = [dict(r) for r in results]
        if diag_store is None:
            diag_snap = None
        else:
            diag_snap = {}
            for disk, tests in diag_store.items():
                diag_snap[disk] = dict(tests)
            for disk, entry in (live_store or {}).items():
                # Копия списка: сэмплер ещё аппендится во время идущего теста,
                # а рендер итерирует снимок в фоновом потоке.
                diag_snap.setdefault(disk, {})[entry["test_id"]] = {
                    "samples": list(entry["samples"]),
                    "summary": {},
                }
    return results_snap, diag_snap


def _write_report(disks, results, diag_store, live_store, state_lock, output_path, tuner,
                  test_names, run_info, fio_configs, show_lat_p99):
    """Перегенерирует MD-отчёт по текущему (возможно, неполному) состоянию."""
    results_snap, diag_snap = _snapshot_state(results, diag_store, live_store, state_lock)
    return generate_report(
        disks, results_snap, test_names, output_path=output_path,
        diag_store=diag_snap,
        tuner_report=tuner.report() if tuner else None,
        run_info=run_info,
        fio_configs=fio_configs,
        show_lat_p99=show_lat_p99,
    )


class _ReportWriter(threading.Thread):
    """Фоновый поток: перегенерирует отчёт по мере поступления данных.

    Реагирует на маркер завершения теста из очереди и, по таймауту,
    на живые посекундные сэмплы идущих тестов (live_store).
    """

    def __init__(self, report_queue, render, has_live, tick=REPORT_TICK):
        super().__init__(daemon=True, name="report-writer")
        self._q = report_queue
        self._render = render
        self._has_live = has_live
        self._tick = tick

    def run(self):
        while True:
            try:
                item = self._q.get(timeout=self._tick)
            except queue.Empty:
                if self._has_live():
                    self._safe_render()
                continue
            if item is _STOP:
                break
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._safe_render()

    def _safe_render(self):
        try:
            self._render()
        except Exception:
            console.print("[dim]Не удалось обновить отчёт[/dim]")


def main():
    args = parse_args()
    validate_configs()

    # Настройка пороговых значений с возможностью переопределения
    thresholds = {
        iface: dict(thr_map)
        for iface, thr_map in INTERFACE_THRESHOLDS.items()
    }
    if args.threshold_nvme:
        thresholds["nvme"].update(parse_custom_thresholds(args.threshold_nvme))
    if args.threshold_sas:
        thresholds["sas"].update(parse_custom_thresholds(args.threshold_sas))
    if args.threshold_sata:
        thresholds["sata"].update(parse_custom_thresholds(args.threshold_sata))

    if args.test:
        # 1. Реальное сканирование (read-only) для предпросмотра оптимизаций.
        #    Ничего не применяется и не запускается — только показ "что будет".
        real_disks = []
        try:
            _, real_disks = scan_disks(INTERFACE_CONFIGS)
        except Exception:
            real_disks = []

        preview_rows = []
        if real_disks and not args.no_tune:
            tuner = SystemTuner(real_disks)
            preview_rows = tuner.preview()
            console.print(
                "[bold]Тестовый режим: предпросмотр оптимизаций "
                "(dry-run, система не меняется)[/bold]"
            )
            if preview_rows:
                for p in preview_rows:
                    if p.get("skipped_reason"):
                        console.print(
                            f"  [yellow]—[/yellow] {p['param']}: "
                            f"пропущено ({p['skipped_reason']})"
                        )
                    else:
                        console.print(
                            f"  [green]✓[/green] {p['param']}: "
                            f"{p['before']} → {p['after']}"
                        )
                    if p.get("target_disks"):
                        console.print(f"      диски: {p['target_disks']}")
            else:
                console.print("  [dim]Оптимизации не требуются[/dim]")

            temps = tuner.get_nvme_temps()
            if temps:
                temp_str = ", ".join(
                    f"{name}: {t:.0f}°C"
                    for name, t in sorted(temps.items())
                    if t is not None
                )
                console.print(f"[bold]Температура NVMe:[/bold] {temp_str}")
            console.print()

        # 2. Фейковая таблица для проверки вёрстки (fio не запускается).
        disks = build_fake_disks()
        if args.add is not None or args.delete is not None:
            disks = apply_disk_selection(disks, args)
            if not disks:
                console.print("[bold yellow]После фильтрации целевые диски не найдены.[/bold yellow]")
                sys.exit(0)
        results = build_fake_results(disks)
        console.print("[bold]Тестовый режим: пробные данные, fio не запускается[/bold]")
        console.print()
        table = build_results_table(disks, results, TEST_NAMES)
        console.print(table)
        report_path = generate_report(
            disks, results, TEST_NAMES, output_path=args.output,
            tuner_report=preview_rows or None,
            run_info=_build_run_info(args),
            fio_configs=_collect_fio_configs(disks),
            show_lat_p99=args.logging,
        )
        console.print(f"[bold green]Отчёт сохранён: {report_path}[/bold green]")
        return

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

    # Ручной выбор дисков: -a/--add или -d/--delete.
    # Если флаг передан без номеров, список уже показан выше — запрашиваем номера.
    if args.add is not None and not args.add:
        args.add = _input_disk_numbers("Номера дисков для теста (через пробел): ")
    elif args.delete is not None and not args.delete:
        args.delete = _input_disk_numbers("Номера дисков для исключения (через пробел): ")

    disks = apply_disk_selection(disks, args)

    if not disks:
        console.print("[bold yellow]После фильтрации целевые диски не найдены.[/bold yellow]")
        sys.exit(0)

    fio_configs = _collect_fio_configs(disks)

    # Состояние заполненности дисков (только при запуске без префилла): читаем
    # сам диск, а не маркеры — после format/blkdiscard диск пуст, после записи
    # (префилл, рабочие данные) содержит данные. Признак попадает в отчёт
    # одной строкой в «Параметры запуска».
    data_state = None
    if not args.prefill:
        states = []
        for d in disks:
            st = disk_has_data(d["name"])
            d["_has_data"] = st
            states.append((d["name"], st))
        data_state = summarize_data_state(states)
        console.print(f"\n[bold]Данные на дисках:[/bold] {data_state}")

    if args.logging:
        for d in disks:
            d["diag_static"] = collect_static_info(d)

    # Проверка наличия fio
    try:
        subprocess.run(["fio", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        console.print("[bold red]ОШИБКА:[/bold red] Утилита fio не найдена. Установите fio:")
        console.print("  apt install fio  (Debian/Ubuntu)")
        console.print("  yum install fio  (RHEL/CentOS)")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]ОШИБКА:[/bold red] fio вернул ошибку: {e}")
        sys.exit(1)

    tuner = None
    if not args.no_tune:
        tuner = SystemTuner(disks, system_disks)
        tuner.detect()
        tuner.apply()
        tuner.print_summary()

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
    diag_store = {} if args.logging else None
    # Живые сэмплы идущих тестов для инкрементального отчёта: {диск: {"test_id", "samples"}}
    live_store = {} if args.logging else None
    if args.logging:
        console.print("\n[bold]Подробное логирование: посекундные сэмплы линка/"
                      "температуры/нагрузки — в единый файл отчёта[/bold]")
        console.print("[dim]Отчёт обновляется по мере завершения тестов[/dim]")

    # Путь отчёта фиксируется один раз: все обновления перезаписывают один файл.
    output_path = Path(args.output) if args.output else _default_report_path()
    state_lock = threading.Lock()
    results = [{} for _ in disks]

    # Начальная запись отчёта: файл существует с самого начала прогона,
    # даже если тесты ещё не начались или прогон прервётся.
    try:
        _write_report(
            disks, results, diag_store, live_store, state_lock, output_path,
            tuner, TEST_NAMES, _build_run_info(args, data_state=data_state),
            fio_configs,
            show_lat_p99=args.logging,
        )
    except Exception as exc:
        console.print(f"[dim]Не удалось создать отчёт: {exc}[/dim]")

    # Фоновый writer: перегенерирует отчёт после каждого теста и по тику
    # показывает живые посекундные сэмплы идущих тестов.
    report_queue = queue.Queue() if args.logging else None
    writer = None
    if args.logging:
        def render_report():
            _write_report(
                disks, results, diag_store, live_store, state_lock, output_path,
                tuner, TEST_NAMES, _build_run_info(args, data_state=data_state),
                fio_configs,
                show_lat_p99=args.logging,
            )
        writer = _ReportWriter(
            report_queue,
            render=render_report,
            has_live=lambda: bool(live_store),
        )
        writer.start()

    # Предварительное заполнение
    prefill_duration = None
    if args.prefill:
        console.print("\n[bold]Предварительное заполнение...[/bold]")
        prefill_duration = prefill_disks(disks, tuner=tuner, cancel_event=cancel_event)

    if args.runtime is not None:
        console.print(f"\n[bold]Длительность теста: {args.runtime} сек[/bold]")

    # Подготовка задач: по одному плану тестов на диск (тесты диска — подряд)
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
        for t, fio_args in tests.items():
            fio_args = list(fio_args)

            # Длительность теста: только если задан явно через -r; иначе
            # остаётся значение runtime= из .fio-конфига.
            if args.runtime is not None:
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
    tests_start = time.monotonic()
    try:
        if args.sequential:
            try:
                for disk_idx, disk, plan in disk_plans:
                    run_disk_tests(
                        disk_idx, disk, plan, results,
                        cancel_event=cancel_event, diag_store=diag_store,
                        tuner=tuner, state_lock=state_lock,
                        report_queue=report_queue, live_store=live_store,
                    )
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
                        tuner=tuner, state_lock=state_lock,
                        report_queue=report_queue, live_store=live_store,
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

        # Вывод результатов (только при успешном завершении всех тестов)
        table = build_results_table(disks, results, TEST_NAMES)
        console.print()
        console.print(table)
    finally:
        # Остановить фоновый writer: живые сэмплы больше не нужны.
        if writer is not None:
            report_queue.put(_STOP)
            writer.join(timeout=5)
        tests_duration = time.monotonic() - tests_start
        console.print(f"\n[bold]Тесты заняли {format_duration(tests_duration)}[/bold]")
        # Финальная запись — либо итоговый отчёт, либо best-effort при сбое.
        try:
            report_path = _write_report(
                disks, results, diag_store, live_store, state_lock, output_path,
                tuner, TEST_NAMES,
                _build_run_info(
                    args,
                    prefill_duration=prefill_duration,
                    tests_duration=tests_duration,
                    data_state=data_state,
                ),
                fio_configs,
                show_lat_p99=args.logging,
            )
        except Exception as exc:
            console.print(f"[bold red]Не удалось записать отчёт:[/bold red] {exc}")
            report_path = None

    if report_path is not None:
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
