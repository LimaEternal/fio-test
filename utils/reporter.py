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


def generate_report(
    disks: List[dict],
    results: List[dict],
    output_path: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Генерирует MD-файл с таблицей результатов.

    Параметры:
        disks       — список словарей с данными дисков
        results     — список словарей с результатами тестов
        output_path — путь для выходного файла (по умолчанию fio_report_<timestamp>.md)

    Возвращает:
        Path к созданному файлу
    """
    if output_path is None:
        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_path = reports_dir / f"fio_report_{timestamp}.md"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []

    lines.append("# Отчёт тестирования накопителей (FIO)")
    lines.append("")
    lines.append(f"> Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    lines.append("## Обнаруженные диски")
    lines.append("")
    lines.append("| Диск | Модель | Серийный номер | Интерфейс | Объём | Физ. сектор |")
    lines.append("|------|--------|----------------|-----------|-------|-------------|")

    for d in disks:
        lines.append(
            f"| /dev/{d['name']} | {d['model']} | {d['serial']} "
            f"| {d['tran']} | {d['size']} | {d['phy_sec']}B |"
        )

    lines.append("")
    lines.append("## Результаты тестирования")
    lines.append("")

    current_disk = None

    for r in results:
        disk_key = r["disk"]

        if disk_key != current_disk:
            current_disk = disk_key
            lines.append(f"### /dev/{disk_key} ({r['model']})")
            lines.append("")
            lines.append(
                "| Тест | Блок | IOPS | Скорость (МБ/с) | Lat Avg (мс) | Lat p99 (мс) | Статус |"
            )
            lines.append(
                "|------|------|------|-----------------|--------------|--------------|--------|"
            )

        status_label = r.get("status", "...")
        if status_label == "done":
            status_display = "done"
        elif status_label == "undone":
            status_display = "undone"
        else:
            status_display = status_label

        if "ERR" in str(r.get("iops", "")):
            lines.append(
                f"| {_strip_rich(r['test_name'])} | {r.get('bs', '—')} | — | — | — | — | {status_display} |"
            )
        else:
            lines.append(
                f"| {_strip_rich(r['test_name'])} | {r.get('bs', '—')} | {r['iops']} | {r['bw']} "
                f"| {r['lat_avg']} | {r['lat_p99']} | {status_display} |"
            )

    lines.append("")
    lines.append("---")
    lines.append(f"*Отчёт сгенерирован автоматически*")

    output_path.write_text("\n".join(lines), encoding="utf-8")

    return output_path
