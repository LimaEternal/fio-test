"""Построение плоской консольной таблицы с результатами FIO.

Внешняя таблица содержит ровно две колонки: паспорт накопителя
(№ + Накопитель) и результаты тестов. Это гарантирует единственный
вертикальный разделитель между группами, а горизонтальные линии
(add_section) разделяют блоки разных дисков.
"""

from rich import box
from rich.table import Table
from rich.text import Text


def format_status(status):
    """Возвращает статус с единственной цветовой разметкой таблицы."""
    if status == "done":
        return Text("done", style="bold green")
    return Text("undone", style="bold red")


def _multiline_cell(header, values, formatter=None):
    """Формирует ячейку с жирной шапкой и строками значений."""
    cell = Text(header, style="bold")
    for value in values:
        cell.append("\n")
        if formatter:
            cell.append_text(formatter(value))
        else:
            cell.append(str(value))
    return cell


def _disk_details(disk):
    """Возвращает строки паспорта накопителя."""
    return (
        f"/dev/{disk['name']}",
        disk.get("model", "N/A").strip(),
        disk.get("tran", "N/A"),
        f"SN: {disk.get('serial', 'N/A').strip()}",
        f"Slot: {disk.get('slot', 'N/A')}",
        f"Размер: {disk.get('size', 'N/A')}",
    )


def _test_columns(disk_results, test_names):
    """Преобразует произвольный набор настроенных тестов в колонки."""
    columns = {
        "names": [],
        "blocks": [],
        "iops": [],
        "bandwidth": [],
        "lat_avg": [],
        "lat_p99": [],
        "statuses": [],
    }

    for test_id, test_name in test_names.items():
        result = disk_results.get(test_id, {})
        columns["names"].append(test_name)
        if "error" in result:
            columns["blocks"].append("—")
            columns["iops"].append("—")
            columns["bandwidth"].append("—")
            columns["lat_avg"].append("—")
            columns["lat_p99"].append("—")
            columns["statuses"].append("undone")
            continue

        columns["blocks"].append(result.get("bs", "4k"))
        columns["iops"].append(f"{result.get('iops', 0):,.0f}")
        columns["bandwidth"].append(f"{result.get('bw_mb', 0):.1f}")
        columns["lat_avg"].append(f"{result.get('lat_avg', 0):.2f}")
        columns["lat_p99"].append(f"{result.get('lat_p99', 0):.2f}")
        columns["statuses"].append(result.get("status", "undone"))

    return columns


def _cell_grid(columns, padding=(0, 1)):
    """Строит grid без рамок из списка (header, values, formatter, justify, min_width)."""
    grid = Table.grid(padding=padding)
    cells = []
    for header, values, formatter, justify, min_width in columns:
        grid.add_column(justify=justify, min_width=min_width)
        cells.append(_multiline_cell(header, values, formatter))
    grid.add_row(*cells)
    return grid


def _passport_cell(index, disk):
    """Строит ячейку паспорта: номер и данные накопителя в одной колонке."""
    details = list(_disk_details(disk))
    lines = [f"{index}. {details[0]}"] + [f"   {line}" for line in details[1:]]
    return _cell_grid([
        ("Накопитель", lines, None, "left", 20),
    ])


def _results_cell(disk_results, test_names):
    """Строит ячейку с результатами всех настроенных тестов."""
    columns = _test_columns(disk_results, test_names)
    return _cell_grid([
        ("Профиль теста", columns["names"], None, "left", 18),
        ("Блок", columns["blocks"], None, "center", None),
        ("IOPS", columns["iops"], None, "right", None),
        ("Скорость (МБ/с)", columns["bandwidth"], None, "right", None),
        ("Lat Avg (мс)", columns["lat_avg"], None, "right", None),
        ("Lat P99 (мс)", columns["lat_p99"], None, "right", None),
        ("Статус", columns["statuses"], format_status, "center", None),
    ], padding=(0, 2))


def build_results_table(disks, results, test_names):
    """Строит одну непрерывную таблицу для всех накопителей."""
    table = Table(
        box=box.ROUNDED,
        expand=True,
        show_header=False,
        border_style="",
        padding=(0, 1),
    )
    table.add_column(min_width=24)
    table.add_column()

    for index, disk in enumerate(disks, 1):
        disk_results = results[index - 1] if index - 1 < len(results) else {}
        table.add_row(
            _passport_cell(index, disk),
            _results_cell(disk_results, test_names),
        )
        table.add_section()

    return table
