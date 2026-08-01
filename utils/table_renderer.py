"""Построение плоской консольной таблицы с результатами FIO."""

from rich import box
from rich.table import Table
from rich.text import Text


COLUMN_HEADERS = (
    "Накопитель",
    "Профиль теста",
    "Блок",
    "IOPS",
    "Скорость (МБ/с)",
    "Lat Avg (мс)",
    "Lat P99 (мс)",
    "Статус",
)


def format_status(status):
    """Возвращает статус с единственной цветовой разметкой таблицы."""
    if status == "done":
        return Text("done", style="bold green")
    return Text("undone", style="bold red")


def _multiline_cell(header, values):
    """Формирует ячейку с названием колонки и строками обычного текста."""
    cell = Text(header, style="bold")
    for value in values:
        cell.append("\n")
        cell.append(str(value))
    return cell


def _status_cell(statuses):
    """Формирует колонку статусов, сохраняя цвет только у значений статуса."""
    cell = Text(COLUMN_HEADERS[-1], style="bold")
    for status in statuses:
        cell.append("\n")
        cell.append_text(format_status(status))
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


def build_results_table(disks, results, test_names):
    """Строит одну непрерывную таблицу для всех накопителей."""
    table = Table(
        box=box.ROUNDED,
        expand=True,
        show_header=False,
        border_style="",
        padding=(0, 1),
    )
    table.add_column(justify="right", width=4)
    table.add_column(min_width=28)
    table.add_column(min_width=18)
    table.add_column(justify="center")
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column(justify="center")

    for index, disk in enumerate(disks, 1):
        disk_results = results[index - 1] if index - 1 < len(results) else {}
        test_columns = _test_columns(disk_results, test_names)
        table.add_row(
            _multiline_cell(COLUMN_HEADERS[0], (index,)),
            _multiline_cell(COLUMN_HEADERS[1], _disk_details(disk)),
            _multiline_cell(COLUMN_HEADERS[2], test_columns["names"]),
            _multiline_cell(COLUMN_HEADERS[3], test_columns["blocks"]),
            _multiline_cell(COLUMN_HEADERS[4], test_columns["iops"]),
            _multiline_cell(COLUMN_HEADERS[5], test_columns["bandwidth"]),
            _multiline_cell(COLUMN_HEADERS[6], test_columns["lat_avg"]),
            _multiline_cell(COLUMN_HEADERS[7], test_columns["lat_p99"]),
            _status_cell(test_columns["statuses"]),
        )

    return table
