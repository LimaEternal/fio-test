"""
Генерация MD-отчёта с результатами тестирования.

Создаёт Markdown-файл с таблицами, удобный для чтения и публикации.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union


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

        lines.append("## Обнаруженные диски")
        lines.append("")
        lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём |")
        lines.append("|------|--------|----------------|-----------|-------|")

        for d in disks:
            pcie_str = ""
            if d.get("pcie_info") and d["pcie_info"].get("gen"):
                pcie_str = f" (PCIe Gen{d['pcie_info']['gen']} x{d['pcie_info'].get('width', '?')})"

            lines.append(
                f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
                f"| {d['tran']}{pcie_str} | {d['size']} |"
            )

        lines.append("")
        lines.append("## Результаты тестирования")
        lines.append("")

        for idx, (disk, disk_results) in enumerate(zip(disks, results), 1):
            lines.append(f"### {idx}. /dev/{disk['name']} ({disk['model']})")
            lines.append("")
            lines.append(
                "| Профиль теста | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Lat P99 (мс) | Статус |"
            )
            lines.append(
                "|---------------|------|------|-----------------|--------------|--------------|--------|"
            )

            for test_id, test_name in test_names.items():
                res = disk_results.get(test_id, {})

                if "error" in res:
                    lines.append(
                        f"| {test_name} | {res.get('bs', '—')} | — | — | — | — | undone |"
                    )
                else:
                    iops = f"{res.get('iops', 0):,.0f}"
                    bw = f"{res.get('bw_mb', 0):.1f}"
                    lat_avg = f"{res.get('lat_avg', 0):.2f}"
                    lat_p99 = f"{res.get('lat_p99', 0):.2f}"
                    status = res.get("status", "undone")
                    lines.append(
                        f"| {test_name} | {res.get('bs', '4k')} | {iops} | {bw} "
                        f"| {lat_avg} | {lat_p99} | {status} |"
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
