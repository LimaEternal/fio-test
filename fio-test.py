"""
fio-test.py — Автоматический бенчмаркинг несистемных накопителей.

Сканирует систему на несистемные диски, классифицирует их по интерфейсу
(NVMe/SAS/SATA), запускает FIO-тесты с оптимальными параметрами для каждого типа
и выводит результаты в реальном времени через rich, а по завершении — в MD-отчёт.
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
from utils.scanner import get_non_system_disks

# Инициализация консоли без цветных тегов и подсветки для совместимости с Jenkins и любыми терминалами
console = Console(color_system=None, highlight=False)

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
            "  python fio-test.py                  — параллельное тестирование (по умолчанию)\n"
            "  python fio-test.py -s               — последовательное тестирование (по одному диску)\n"
            "  python fio-test.py --precond        — с прекондишнингом\n"
            "  python fio-test.py --output my.md   — свой путь для отчёта\n"
            "  python fio-test.py --threshold-nvme 2000,1500,100000,80000\n"
        ),
    )

    parser.add_argument(
        "-s", "--sequential",
        action="store_true",
        help=(
            "Запускать тесты последовательно (диск за диском) вместо параллельного. "
            "Рекомендуется для виртуальных машин и дисков с общим HBA-контроллером."
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
        console.print("Ошибка: нужно ровно 4 значения через запятую")
        sys.exit(1)

    try:
        seq_read_bw, seq_write_bw, rand_read_iops, rand_write_iops = [float(v) for v in vals]
    except ValueError:
        console.print("Ошибка: все значения должны быть числами")
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
        console.print("INTERFACE_CONFIGS должен быть словарём")
        sys.exit(1)

    required_test_keys = {"id", "name", "args"}
    required_args = {"--rw=", "--bs=", "--runtime=", "--time_based"}
    valid_bs = {"512", "1k", "2k", "4k", "8k", "16k", "32k", "64k", "128k",
                "256k", "512k", "1m"}

    for iface, tests in INTERFACE_CONFIGS.items():
        if not isinstance(tests, list) or len(tests) == 0:
            console.print(f"Ошибка в конфиге {iface}: TESTS должен быть непустым списком")
            sys.exit(1)

        for idx, test in enumerate(tests):
            missing = required_test_keys - set(test.keys())
            if missing:
                console.print(f"Ошибка в конфиге {iface}, тест {idx}: отсутствуют ключи {missing}")
                sys.exit(1)

            args = test["args"]
            if not isinstance(args, list):
                console.print(f"Ошибка в конфиге {iface}, тест {test['id']}: args должен быть списком")
                sys.exit(1)

            for arg in args:
                if not any(arg.startswith(req) for req in required_args):
                    if arg in ("--time_based",):
                        continue
                    if not arg.startswith("--") and not arg.startswith("-"):
                        console.print(
                            f"Ошибка в конфиге {iface}, тест {test['id']}: "
                            f"неожиданный аргумент '{arg}'"
                        )
                        sys.exit(1)

            bs_val = None
            for arg in args:
                if arg.startswith("--bs="):
                    bs_val = arg.split("=", 1)[1].lower()
                    break

            if bs_val is None:
                console.print(
                    f"Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"отсутствует параметр --bs"
                )
                sys.exit(1)

            if bs_val not in valid_bs:
                console.print(
                    f"Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"некорректный bs='{bs_val}'"
                )
                sys.exit(1)

            if not any(a.startswith("--runtime=") for a in args):
                console.print(
                    f"Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"отсутствует --runtime"
                )
                sys.exit(1)

            if "--time_based" not in args:
                console.print(
                    f"Ошибка в конфиге {iface}, тест {test['id']}: "
                    f"отсутствует --time_based"
                )
                sys.exit(1)

    for iface, thresholds in INTERFACE_THRESHOLDS.items():
        if not isinstance(thresholds, dict):
            console.print(f"Ошибка в конфиге {iface}: THRESHOLDS должен быть словарём")
            sys.exit(1)

        if iface not in INTERFACE_CONFIGS:
            console.print(f"Ошибка в конфиге {iface}: интерфейс не найден в INTERFACE_CONFIGS")
            sys.exit(1)

        test_ids = {t["id"] for t in INTERFACE_CONFIGS[iface]}
        for tid, vals in thresholds.items():
            if tid not in test_ids:
                console.print(
                    f"Ошибка в THRESHOLDS[{iface}]: тест '{tid}' не найден в TESTS"
                )
                sys.exit(1)
            if "min_bw_mb" not in vals and "min_iops" not in vals:
                console.print(
                    f"Ошибка в THRESHOLDS[{iface}]['{tid}']: "
                    f"нужен min_bw_mb или min_iops"
                )
                sys.exit(1)

        if len(thresholds) != len(test_ids):
            console.print(
                f"Ошибка в THRESHOLDS[{iface}]: "
                f"количество порогов ({len(thresholds)}) != количество тестов ({len(test_ids)})"
            )
            sys.exit(1)

    for iface, desc in INTERFACE_DESCRIPTIONS.items():
        if not isinstance(desc, str) or not desc.strip():
            console.print(f"Ошибка в конфиге {iface}: DESCRIPTION должен быть непустой строкой")
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

    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return {"error": "Утилита 'fio' не установлена в системе. Пожалуйста, установите 'fio' (например, 'apt install fio')."}
    except Exception as e:
        return {"error": f"Не удалось запустить утилиту FIO: {e}"}

    if res.returncode != 0:
        stderr_hint = f" ({res.stderr.strip()})" if res.stderr else ""
        return {"error": f"FIO завершился с ошибкой (код {res.returncode}){stderr_hint}"}

    try:
        fio_data = json.loads(res.stdout)
        if "jobs" not in fio_data or not fio_data["jobs"]:
            return {"error": "Вывод FIO не содержит результатов выполнения работы (jobs)."}
            
        job = fio_data["jobs"][0]
        mode = "read" if "read" in test_id else "write"
        
        if mode not in job:
            return {"error": f"В результатах FIO отсутствует статистика для режима '{mode}'."}
            
        stats = job[mode]

        iops = int(stats.get("iops", 0))
        bw = float(stats.get("bw", 0))
        
        lat_ns = stats.get("lat_ns", {})
        avg_lat = lat_ns.get("mean", 0) / 1_000_000
        
        clat_ns = stats.get("clat_ns", {})
        percentiles = clat_ns.get("percentile", {})
        p99_lat = percentiles.get("99.000000", 0) / 1_000_000

        return {
            "iops": iops,
            "bw_mb": round(bw / 1024, 2),
            "lat_avg": round(avg_lat, 2),
            "lat_p99": round(p99_lat, 2),
        }
    except json.JSONDecodeError:
        return {"error": "Не удалось декодировать JSON-вывод FIO. Возможно, запуск прервался или вывод поврежден."}
    except KeyError as e:
        return {"error": f"В выводе FIO отсутствует ожидаемый ключ статистики: {e}"}
    except Exception as e:
        return {"error": f"Ошибка обработки результатов FIO: {e}"}


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

    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode == 0
    except FileNotFoundError:
        console.print("Ошибка: утилита 'fio' не найдена. Пожалуйста, установите 'fio' в систему.")
        return False
    except Exception as e:
        console.print(f"Непредвиденная ошибка при запуске прекондишинга: {e}")
        return False


def optimize_nvme_args(test_id: str, args_list: list[str], pcie_info: dict) -> list[str]:
    """
    Динамически переопределяет параметры запуска FIO в зависимости от поколения PCIe.
    Это помогает раскрыть потенциал высокопроизводительных Gen5 дисков и избежать 
    бутылочного горлышка в виде CPU (однопоточных ограничений).
    """
    new_args = list(args_list)
    gen = pcie_info.get("gen")
    
    if not gen:
        return new_args

    def set_arg(prefix: str, val: str):
        for idx, arg in enumerate(new_args):
            if arg.startswith(prefix):
                new_args[idx] = f"{prefix}{val}"
                return
        new_args.append(f"{prefix}{val}")

    if gen >= 5:
        if test_id == "seq_read":
            set_arg("--numjobs=", "4")
            set_arg("--iodepth=", "16")
            set_arg("--bs=", "256k")
        elif test_id == "seq_write":
            set_arg("--numjobs=", "2")
            set_arg("--iodepth=", "16")
        elif test_id == "rand_read":
            set_arg("--numjobs=", "16")
            set_arg("--iodepth=", "16")
        elif test_id == "rand_write":
            set_arg("--numjobs=", "8")
            set_arg("--iodepth=", "16")
    elif gen == 4:
        if test_id == "seq_read":
            set_arg("--numjobs=", "2")
            set_arg("--iodepth=", "16")
        elif test_id == "rand_read":
            set_arg("--numjobs=", "8")
            set_arg("--iodepth=", "32")
            
    return new_args


_HLINE_BOX = None
try:
    from rich.box import SIMPLE as _HLINE_BOX
except (ImportError, Exception):
    pass


def _status_style(status: str) -> str:
    """Возвращает чистую строку статуса без цветовых тегов."""
    return status


def build_results_table(disks: list, results: list) -> Table:
    """Строит общую внешнюю таблицу без цветовых стилей (чистый белый текст)."""
    grouped = []
    for disk in disks:
        disk_results = [r for r in results if r["disk"] == disk["name"]]
        if disk_results:
            grouped.append((disk, disk_results))

    outer = Table(
        show_header=True,
        show_edge=True,
        show_lines=True,
        padding=(1, 1),
    )
    outer.add_column("№", justify="center", width=4)
    outer.add_column("Накопитель", justify="left", width=35)
    outer.add_column("Результаты тестирования накопителей (FIO)", justify="left")

    for disk_num, (disk, disk_results) in enumerate(grouped, 1):
        slot_str = f"Slot: {disk['slot']}\n" if disk.get("slot") else ""
        pcie_str = ""
        if disk.get("pcie_info") and disk["pcie_info"].get("gen"):
            pcie_str = f" (PCIe Gen{disk['pcie_info']['gen']} x{disk['pcie_info'].get('width', '?')})"
        
        disk_info_text = (
            f"/dev/{disk['name']}\n"
            f"{disk['model']}\n"
            f"{disk['tran']}{pcie_str}\n"
            f"SN: {disk['serial']}\n"
            f"{slot_str}"
            f"Размер: {disk['size']}"
        )

        inner = Table(
            box=_HLINE_BOX,
            show_edge=False,
            show_lines=True,
            padding=(0, 2),
        )
        inner.add_column("Профиль теста")
        inner.add_column("Блок", justify="right")
        inner.add_column("IOPS", justify="right")
        inner.add_column("Скорость (МБ/с)", justify="right")
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

        outer.add_row(str(disk_num), disk_info_text, inner)

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

    console.print("Сканирование системы на несистемные диски...")
    
    try:
        disks = get_non_system_disks(INTERFACE_CONFIGS)
    except Exception as e:
        console.print(f"\nКритическая ошибка при сканировании устройств:")
        console.print(f"{e}\n")
        sys.exit(1)

    if not disks:
        console.print(
            "Безопасные несистемные диски для тестов не найдены!\n"
            "Все подключенные диски содержат активные системные разделы (/, /boot и др.) и были отфильтрованы для защиты данных."
        )
        sys.exit(1)

    console.print(
        f"Обнаружено целевых дисков: {len(disks)}\n"
    )

    for i, d in enumerate(disks, 1):
        slot_str = f", Slot: {d['slot']}" if d.get("slot") else ""
        pcie_str = ""
        if d.get("pcie_info") and d["pcie_info"].get("gen"):
            pcie_str = f", PCIe Gen{d['pcie_info']['gen']} x{d['pcie_info'].get('width', '?')}"
            
        console.print(
            f"  {i}. /dev/{d['name']} — {d['model']} "
            f"({d['tran']}{pcie_str}, SN: {d['serial']}{slot_str})"
        )
    console.print()

    if args.precond:
        console.print(
            "Прекондишнинг: запись 100% объёма каждого диска (bs=1M, --direct=1)...\n"
        )

        for disk in disks:
            console.print(
                f"  Прекондишнинг /dev/{disk['name']}..."
            )
            ok = run_precondition(disk)
            if ok:
                console.print(
                    f"  ✓ /dev/{disk['name']} готов"
                )
            else:
                console.print(
                    f"  ✗ Ошибка прекондишинга /dev/{disk['name']}"
                )
        console.print()

    if args.runtime != 30:
        console.print(
            f"Длительность теста: {args.runtime}с\n"
        )

    mode_desc = "последовательный (-s)" if args.sequential else "параллельный"
    console.print(f"Выполнение бенчмарка (режим: {mode_desc})...")

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
            
            if disk["tran"] == "NVME" and disk.get("pcie_info"):
                fio_args = optimize_nvme_args(t["id"], fio_args, disk["pcie_info"])

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

    def process_task_result(idx, disk, t, fio_args, res):
        bs = "4k"
        for a in fio_args:
            if a.startswith("--bs="):
                bs = a.split("=", 1)[1]
                break

        if "error" in res:
            results[idx]["test_name"] = f"{t['name']} (ERR)"
            results[idx]["iops"] = "ERR"
            results[idx]["bw"] = "—"
            results[idx]["lat_avg"] = "—"
            results[idx]["lat_p99"] = "—"
            results[idx]["error_msg"] = res["error"]
            results[idx]["bs"] = bs
            results[idx]["status"] = "undone"
            console.print(f"Ошибка теста '{t['name']}' на /dev/{disk['name']}: {res['error']}")
        else:
            results[idx]["test_name"] = t["name"]
            results[idx]["iops"] = f"{res['iops']:,}"
            results[idx]["bw"] = res["bw_mb"]
            results[idx]["lat_avg"] = res["lat_avg"]
            results[idx]["lat_p99"] = res["lat_p99"]
            results[idx]["error_msg"] = None
            results[idx]["bs"] = bs
            results[idx]["status"] = check_threshold(
                t["id"], res, thresholds[disk["tran"]]
            )

    if args.sequential:
        # Последовательный запуск тестов по одному
        for idx, disk, t, fio_args in tasks:
            res = run_fio_test(disk, t["id"], fio_args)
            process_task_result(idx, disk, t, fio_args, res)
    else:
        # Параллельный запуск тестов на всех дисках (по умолчанию)
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
                try:
                    res = future.result()
                except Exception as e:
                    res = {"error": f"Непредвиденный сбой потока выполнения теста: {e}"}

                process_task_result(idx, disk, t, fio_args, res)

    console.print()
    console.print("Результаты тестирования накопителей (FIO)")
    
    try:
        console.print(build_results_table(disks, results))
    except Exception as e:
        console.print(f"Ошибка построения результирующей таблицы: {e}")
        
    console.print("\nВсе тесты завершены.")

    try:
        report_path = generate_report(disks, results, args.output)
        console.print(f"Отчёт сохранён: {report_path}")
    except Exception as e:
        console.print(f"Не удалось сгенерировать или сохранить MD-отчёт: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\nТестирование прервано пользователем.")
        sys.exit(1)
    except Exception as e:
        console.print(f"\nПроизошел непредвиденный сбой скрипта: {e}")
        sys.exit(1)
