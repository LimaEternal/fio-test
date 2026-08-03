"""
Генерация MD-отчёта с результатами тестирования.

Создаёт Markdown-файл с таблицами, удобный для чтения и публикации.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from utils.table_renderer import SHOW_LAT_P99, _fmt


def _strip_rich(text: str) -> str:
    """Удаляет rich-разметку [tag]...[/tag] из строки."""
    return re.sub(r"\[.*?\]", "", text)


TEST_NAMES = {
    "seq_read": "1. Послед. Чтение",
    "seq_write": "2. Послед. Запись",
    "rand_read": "3. Случ. Чтение 4k",
    "rand_write": "4. Случ. Запись 4k",
}


def generate_report(
    disks: List[dict],
    results: List[dict],
    test_names: Optional[dict] = None,
    output_path: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Генерирует MD-файл с таблицей результатов.

    Параметры:
        disks       — список словарей с данными дисков
        results     — список словарей с результатами тестов (по одному на диск)
        test_names  — порядок и отображаемые имена тестов (по умолчанию TEST_NAMES)
        output_path — путь для выходного файла (по умолчанию fio_report_<timestamp>.md)

    Возвращает:
        Path к созданному файлу
    """
    if test_names is None:
        test_names = TEST_NAMES
    try:
        if output_path is None:
            reports_dir = Path("reports")
            reports_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            output_path = reports_dir / f"fio_report_{timestamp}.md"
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = []

        lines.append("# Результаты тестирования накопителей (FIO)")
        lines.append("")
        lines.append(f"> Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        has_diag = (
            any(d.get("diag_static") for d in disks)
            or any(
                res.get("diag")
                for disk_results in results
                for res in disk_results.values()
            )
        )

        lines.append("## Обнаруженные диски")
        lines.append("")
        if has_diag:
            lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём | NUMA | CPU-аффинити |")
            lines.append("|------|--------|----------------|-----------|-------|------|--------------|")
        else:
            lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём |")
            lines.append("|------|--------|----------------|-----------|-------|")

        for d in disks:
            pcie_str = ""
            if d.get("pcie_info") and d["pcie_info"].get("gen"):
                pcie_str = f" (PCIe Gen{d['pcie_info']['gen']} x{d['pcie_info'].get('width', '?')})"

            if has_diag:
                static = d.get("diag_static") or {}
                numa = static.get("numa_node") or "—"
                affinity = static.get("cpu_affinity") or "—"
                lines.append(
                    f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                    f"| {d['tran'].upper()}{pcie_str} | {d['size']} | {numa} | {affinity} |"
                )
            else:
                lines.append(
                    f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                    f"| {d['tran'].upper()}{pcie_str} | {d['size']} |"
                )

        lines.append("")
        lines.append("## Результаты тестирования")
        lines.append("")

        for idx, (disk, disk_results) in enumerate(zip(disks, results), 1):
            lines.append(f"### {idx}. /dev/{disk['name']} ({disk['model']})")
            lines.append("")
            if SHOW_LAT_P99:
                lines.append(
                    "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Lat P99 (мс) | Статус |"
                )
                lines.append(
                    "|---------------|------|------|-----------------|--------------|--------------|--------|"
                )
            else:
                lines.append(
                    "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Статус |"
                )
                lines.append(
                    "|---------------|------|------|-----------------|--------------|--------|"
                )

            for test_id, test_name in test_names.items():
                res = disk_results.get(test_id, {})

                if "error" in res:
                    cells = [test_name, res.get("bs", "—"), "—", "—", "—"]
                    if SHOW_LAT_P99:
                        cells.append("—")
                    cells.append("undone")
                else:
                    cells = [
                        test_name,
                        res.get("bs", "4k"),
                        _fmt(res.get("iops", 0), ",.0f"),
                        _fmt(res.get("bw_mb", 0), ".1f"),
                        _fmt(res.get("lat_avg", 0), ".2f"),
                    ]
                    if SHOW_LAT_P99:
                        cells.append(_fmt(res.get("lat_p99", 0), ".2f"))
                    cells.append(res.get("status", "undone"))
                lines.append("| " + " | ".join(cells) + " |")

            lines.append("")

            if has_diag:
                lines.append(f"**Диагностика — {disk['name']}**")
                lines.append("")
                lines.append(
                    "| Тест | CPU user/sys (%) | Линк min | Tmax (°C) | avgqu-sz | "
                    "clat p99/p99.9 (мс) | slat (мс) | iodepth | Объём (ГБ) |"
                )
                lines.append(
                    "|------|------------------|----------|-----------|----------|"
                    "--------------------|-----------|---------|-------------|"
                )
                for test_id, test_name in test_names.items():
                    res = disk_results.get(test_id, {})
                    if "error" in res:
                        continue
                    diag = res.get("diag") or {}

                    cpu = f"{res.get('cpu_user', '—')} / {res.get('cpu_sys', '—')}"

                    link = "—"
                    if diag.get("link_gts_min") is not None:
                        width = diag.get("link_width_min") or "?"
                        link = f"{diag['link_gts_min']:g} GT/s x{width}"

                    tmax = _fmt(diag["temp_max_c"], ".1f") if diag.get("temp_max_c") is not None else "—"
                    qu = _fmt(diag["avgqu_sz_max"], ".1f") if diag.get("avgqu_sz_max") is not None else "—"

                    p99 = res.get("clat_p99_ms", "—")
                    p999 = res.get("clat_p99_9_ms", "—")
                    clat = f"{p99} / {p999}" if p99 != "—" else "—"

                    iod = res.get("iodepth", "—")
                    io_kb = res.get("io_kb")
                    gb = _fmt(io_kb / 1024 / 1024, ".1f") if io_kb else "—"

                    lines.append(
                        f"| {test_name} | {cpu} | {link} | {tmax} | {qu} | "
                        f"{clat} | {res.get('slat_avg_ms', '—')} | {iod} | {gb} |"
                    )
                lines.append("")

        lines.append("---")
        lines.append("*Отчёт сгенерирован автоматически*")

        output_path.write_text("\n".join(lines), encoding="utf-8")

        return output_path
    except PermissionError:
        raise RuntimeError(f"Отказано в доступе при попытке записи отчёта по пути '{output_path}'.")
    except Exception as e:
        raise RuntimeError(f"Ошибка при создании файла отчёта '{output_path}': {e}")
