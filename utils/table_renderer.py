"""Построение консольных таблиц с результатами FIO."""

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text


TEST_ORDER = ("seq_read", "seq_write", "rand_read", "rand_write")
DEFAULT_GAP = 0
DISK_TABLE_BOX = box.ROUNDED
RESULTS_TABLE_BOX = box.ROUNDED


def format_status(status):
    """Возвращает статус с единственной цветовой разметкой таблицы."""
    if status == "done":
        return Text("done", style="bold green")
    return Text("undone", style="bold red")


def build_disk_info(disk):
    """Формирует многострочный паспорт накопителя без цветовых стилей."""
    lines = [
        f"/dev/{disk['name']}",
        disk.get("model", "N/A").strip(),
        disk.get("tran", "N/A"),
        f"SN: {disk.get('serial', 'N/A').strip()}",
        f"Slot: {disk.get('slot', 'N/A')}",
        f"Размер: {disk.get('size', 'N/A')}",
    ]
    return Text("\n".join(lines))


def build_test_results_table(disk_results, test_names):
    """Строит замкнутую таблицу результатов одного накопителя."""
    table = Table(
        box=RESULTS_TABLE_BOX,
        show_header=True,
        header_style="",
        border_style="",
        padding=(0, 1),
    )
    table.add_column("Профиль теста", min_width=18)
    table.add_column("Блок", justify="center")
    table.add_column("IOPS", justify="right")
    table.add_column("Скорость (МБ/с)", justify="right")
    table.add_column("Lat Avg (мс)", justify="right")
    table.add_column("Lat P99 (мс)", justify="right")
    table.add_column("Статус", justify="center")

    for test_id in TEST_ORDER:
        result = disk_results.get(test_id, {})
        test_name = test_names.get(test_id, test_id)
        if "error" in result:
            table.add_row(
                test_name, "—", "—", "—", "—", "—", format_status("undone")
            )
            continue

        table.add_row(
            test_name,
            result.get("bs", "4k"),
            f"{result.get('iops', 0):,.0f}",
            f"{result.get('bw_mb', 0):.1f}",
            f"{result.get('lat_avg', 0):.2f}",
            f"{result.get('lat_p99', 0):.2f}",
            format_status(result.get("status", "undone")),
        )

    return table


def build_disk_table(index, disk, disk_results, test_names):
    """Строит самостоятельный рамочный блок одного накопителя."""
    table = Table(
        box=DISK_TABLE_BOX,
        expand=True,
        show_header=True,
        header_style="",
        border_style="",
        padding=(0, 1),
    )
    table.add_column("№", justify="right", width=4)
    table.add_column("Накопитель", min_width=30)
    table.add_column(
        "Результаты тестирования накопителя (FIO)", min_width=70
    )
    table.add_row(
        str(index),
        build_disk_info(disk),
        build_test_results_table(disk_results, test_names),
    )
    return table


def build_results_table(disks, results, test_names, gap=DEFAULT_GAP):
    """Объединяет самостоятельные таблицы дисков в один Rich renderable."""
    renderables = []
    for index, disk in enumerate(disks, 1):
        if renderables and gap > 0:
            renderables.append(Text("\n" * (gap - 1)))
        disk_results = results[index - 1] if index - 1 < len(results) else {}
        renderables.append(
            build_disk_table(index, disk, disk_results, test_names)
        )
    return Group(*renderables)
