"""Построение плоской консольной таблицы с результатами FIO.

Внешняя таблица — три колонки (№, Накопитель, результаты тестов)
с единой шапкой и тонкими линиями (box.ROUNDED). Строки дисков
разделяются горизонтальными линиями (add_section), результаты
собраны во вложенную таблицу с собственным заголовком, переносом
колонок и данными, отцентрированными под заголовками.
"""

from rich import box
from rich.table import Table
from rich.text import Text


TITLE = "Результаты тестирования накопителей (FIO)"

# Поставьте True, чтобы вернуть колонку Lat p99
# (метрика по-прежнему парсится в fio-test.py, скрыт только вывод).
SHOW_LAT_P99 = False

RESULT_HEADERS = tuple(
    [("Профиль теста", "left", 9), ("Блок", "center", 5), ("IOPS", "center", 7),
     ("Скорость (МБ/с)", "center", 8), ("Lat Avg (мс)", "center", 11)]
    + ([("Lat p99 (мс)", "center", 10)] if SHOW_LAT_P99 else [])
    + [("Статус", "center", 6)]
)


def format_status(status):
    """Возвращает статус с цветовой разметкой; неизвестные значения — как есть."""
    if status == "done":
        return Text("done", style="bold green")
    if status == "undone":
        return Text("undone", style="bold red")
    return Text(str(status))


def _disk_details(disk):
    """Возвращает строки паспорта накопителя."""
    return (
        f"/dev/{disk['name']}",
        disk.get("model", "N/A").strip(),
        disk.get("tran", "N/A").upper(),
        f"SN: {disk.get('serial', 'N/A').strip()}",
        f"Slot: {disk.get('slot', 'N/A')}",
        f"Размер: {disk.get('size', 'N/A')}",
    )


def _fmt(value, spec):
    """Форматирует число; строки (например, 'test') пропускает как есть."""
    try:
        return f"{float(value):{spec}}"
    except (TypeError, ValueError):
        return str(value)


def _test_rows(disk_results, test_names):
    """Преобразует результаты тестов в строки вложенной таблицы.

    Имя теста завершается переводом строки: перенос имени даёт
    пустую строку после каждого теста (включая последний).
    """
    rows = []
    for test_id, test_name in test_names.items():
        result = disk_results.get(test_id, {})
        if "error" in result:
            row = [test_name + "\n", "—", "—", "—", "—"]
            if SHOW_LAT_P99:
                row.append("—")
            row.append("undone")
        else:
            row = [
                test_name + "\n",
                result.get("bs", "4k"),
                _fmt(result.get("iops", 0), ",.0f"),
                _fmt(result.get("bw_mb", 0), ".1f"),
                _fmt(result.get("lat_avg", 0), ".2f"),
            ]
            if SHOW_LAT_P99:
                row.append(_fmt(result.get("lat_p99", 0), ".2f"))
            row.append(result.get("status", "undone"))
        rows.append(tuple(row))
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
        box=box.ROUNDED,
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
