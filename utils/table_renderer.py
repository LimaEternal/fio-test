"""Построение плоской консольной таблицы с результатами FIO.

Внешняя таблица — три колонки (№, Накопитель, результаты тестов)
с единой шапкой. Линия под шапкой убрана (HEAVY_HEAD_NO_LINE),
строки дисков разделяются горизонтальными линиями (add_section),
результаты собраны во вложенную таблицу с собственным заголовком,
переносом колонок и данными, отцентрированными под заголовками.
"""

from rich import box
from rich.box import Box
from rich.table import Table
from rich.text import Text


TITLE = "Результаты тестирования накопителей (FIO)"

HEAVY_HEAD_NO_LINE = Box(
    "┏━┳┓\n"
    "┃ ┃┃\n"
    "    \n"
    "│ ││\n"
    "├─┼┤\n"
    "├─┼┤\n"
    "│ ││\n"
    "└─┴┘"
)

RESULT_HEADERS = (
    ("Профиль теста", "left", 9),
    ("Блок", "center", 5),
    ("IOPS", "center", 7),
    ("Скорость (МБ/с)", "center", 8),
    ("Lat Avg (мс)", "center", 11),
    ("Lat p99 (мс)", "center", 10),
    ("Статус", "center", 6),
)


def format_status(status):
    """Возвращает статус с единственной цветовой разметкой таблицы."""
    if status == "done":
        return Text("done", style="bold green")
    return Text("undone", style="bold red")


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


def _test_rows(disk_results, test_names):
    """Преобразует результаты тестов в строки вложенной таблицы.

    Имя теста завершается переводом строки: перенос имени даёт
    пустую строку после каждого теста (включая последний).
    """
    rows = []
    for test_id, test_name in test_names.items():
        result = disk_results.get(test_id, {})
        if "error" in result:
            rows.append((test_name + "\n", "—", "—", "—", "—", "—", "undone"))
        else:
            rows.append((
                test_name + "\n",
                result.get("bs", "4k"),
                f"{result.get('iops', 0):,.0f}",
                f"{result.get('bw_mb', 0):.1f}",
                f"{result.get('lat_avg', 0):.2f}",
                f"{result.get('lat_p99', 0):.2f}",
                result.get("status", "undone"),
            ))
    return rows


def _results_cell(disk_results, test_names):
    """Строит вложенную таблицу результатов с собственным заголовком."""
    sub = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="",
        padding=0,
        collapse_padding=True,
    )
    for header, justify, min_width in RESULT_HEADERS:
        sub.add_column(
            header=header, justify=justify,
            min_width=min_width, overflow="fold",
        )

    for row in _test_rows(disk_results, test_names):
        sub.add_row(*row[:-1], format_status(row[-1]))

    return sub


def build_results_table(disks, results, test_names):
    """Строит одну непрерывную таблицу для всех накопителей."""
    table = Table(
        box=HEAVY_HEAD_NO_LINE,
        show_header=True,
        header_style="",
        border_style="",
        padding=(0, 1),
    )
    table.add_column(header="\n№\n", justify="center", width=3)
    table.add_column(header="\nНакопитель\n", min_width=16, overflow="fold")
    table.add_column(header=f"\n{TITLE}\n")

    for index, disk in enumerate(disks, 1):
        disk_results = results[index - 1] if index - 1 < len(results) else {}
        table.add_row(
            Text(f"\n{index}"),
            Text("\n" + "\n".join(_disk_details(disk))),
            _results_cell(disk_results, test_names),
        )
        table.add_section()

    return table
