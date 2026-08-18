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

from utils.hw_profile import link_bandwidth_mbps


TITLE = "Результаты тестирования накопителей (FIO)"

SHOW_LAT_P99 = False

# Базовая раскладка: колонки Tmax/Tavg (°C) показываются всегда,
# Lat p99 — только в подробном режиме (-l), где добавляется динамически
# (см. _result_headers).
BASE_RESULT_HEADERS = tuple(
    [("Профиль теста", "left", 9), ("Блок", "center", 5), ("IOPS", "center", 7),
     ("Скорость (МБ/с)", "center", 8), ("Lat Avg (мс)", "center", 11)]
    + [("Tmax (°C)", "center", 8), ("Tavg (°C)", "center", 8)]
    + [("Статус", "center", 6)]
)

RESULT_HEADERS = BASE_RESULT_HEADERS


def _result_headers():
    """Колонки вложенной таблицы с учётом текущего режима отображения."""
    headers = list(BASE_RESULT_HEADERS)
    if SHOW_LAT_P99:
        headers.insert(-3, ("Lat p99 (мс)", "center", 10))
    return headers


def format_status(status):
    """Возвращает статус с цветовой разметкой; неизвестные значения — как есть."""
    if status == "PASS":
        return Text("PASS", style="bold green")
    if status == "FAIL":
        return Text("FAIL", style="bold red")
    return Text(str(status))


def _disk_link_lines(profile):
    """Возвращает строки линка/поколения PCIe для паспорта накопителя.

    Добавляет поколение интерфейса, теоретическую пропускную
    способность шины, MaxPayload (только NVMe — PCIe-уровень) и
    статус downgrade. Ключевое слово 'downgrade' пишется ВСЕГДА,
    далее через ':' — есть/нет/?. Для SAS/SATA MaxPayload неприменим
    (шина PCIe относится к HBA-контроллеру, а не к самому диску).
    """
    interface = (profile.get("interface") or "").lower()
    link = profile.get("link")
    if not link:
        return ()

    lines = []
    if interface == "nvme":
        gen = link.get("gen")
        width = link.get("width")
        max_gen = link.get("max_gen")
        if gen:
            lines.append(f"PCIe {gen} x{width}" if width else f"PCIe {gen}")
        bw = link_bandwidth_mbps("nvme", link)
        if bw is not None:
            lines.append(f"Пропускная: {bw:.0f} МБ/с")
        # MaxPayload (PCIe-уровень, только для NVMe)
        mp = link.get("max_payload")
        if isinstance(mp, dict) and mp.get("device"):
            dev = mp["device"]
            port = mp.get("port")
            if port and dev <= 128:
                lines.append(
                    f"⚠ MaxPayload: устройство {dev}B < порт {port}B "
                    f"(лимитит пропускную способность)"
                )
            else:
                lines.append(f"MaxPayload: {dev}B")
        else:
            lines.append("MaxPayload: N/A")
        # downgrade (ключевое слово пишем всегда)
        if gen and max_gen:
            if max_gen > gen:
                lines.append(
                    f"downgrade: есть (накопитель Gen{max_gen}, порт Gen{gen})"
                )
            else:
                lines.append("downgrade: нет")
        else:
            lines.append("downgrade: ?")
    elif interface == "sas":
        neg = link.get("negotiated_gbps")
        max_l = link.get("maximum_gbps")
        if neg:
            lines.append(f"SAS {neg:.0f} Gbps")
        bw = link_bandwidth_mbps("sas", link)
        if bw is not None:
            lines.append(f"Пропускная: {bw:.0f} МБ/с")
        lines.append("MaxPayload: HBA Only")
        if neg and max_l:
            if max_l > neg:
                lines.append(
                    f"downgrade: есть (порт {neg:.0f} Gbps, накопитель {max_l:.0f} Gbps)"
                )
            else:
                lines.append("downgrade: нет")
        else:
            lines.append("downgrade: ?")
    elif interface == "sata":
        spd = link.get("spd_limit_gbps")
        hw = link.get("hw_spd_limit_gbps")
        if spd:
            lines.append(f"SATA {spd:.0f} Gbps")
        bw = link_bandwidth_mbps("sata", link)
        if bw is not None:
            lines.append(f"Пропускная: {bw:.0f} МБ/с")
        lines.append("MaxPayload: HBA Only")
        if spd and hw:
            if hw > spd:
                lines.append(
                    f"downgrade: есть (порт {spd:.0f} Gbps, накопитель {hw:.0f} Gbps)"
                )
            else:
                lines.append("downgrade: нет")
        else:
            lines.append("downgrade: ?")

    return tuple(lines)


def _disk_details(disk):
    """Возвращает строки паспорта накопителя."""
    lines = [
        f"/dev/{disk['name']}",
        disk.get("model", "N/A").strip(),
        disk.get("tran", "N/A").upper(),
        f"SN: {disk.get('serial', 'N/A').strip()}",
        f"Slot: {disk.get('slot', 'N/A')}",
        f"Размер: {disk.get('size', 'N/A')}",
    ]
    profile = disk.get("profile")
    if profile:
        lines.extend(_disk_link_lines(profile))
    return tuple(lines)


def _fmt(value, spec):
    """Форматирует число; строки (например, 'test') пропускает как есть."""
    try:
        return f"{float(value):{spec}}"
    except (TypeError, ValueError):
        return str(value)


def _test_rows(disk_results, test_names):
    """Преобразует результаты тестов в строки вложенной таблицы."""
    rows = []
    for test_id, test_name in test_names.items():
        result = disk_results.get(test_id, {})
        if "error" in result:
            row = [test_name + "\n", "—", "—", "—", "—"]
            if SHOW_LAT_P99:
                row.append("—")
            row.append("—")
            row.append("FAIL")
        else:
            row = [
                test_name + "\n",
                result.get("bs", "4k"),
                _fmt(result.get("iops", 0), ",.0f"),
                _fmt(result.get("bw_mb", 0), ".1f"),
                _fmt(result.get("lat_avg", 0), ".2f"),
            ]
            if SHOW_LAT_P99:
                if result.get("lat_p99_unreliable"):
                    row.append("—")
                else:
                    row.append(_fmt(result.get("lat_p99", 0), ".2f"))
            diag = result.get("diag") or {}
            row.append(_fmt(diag.get("temp_max_c"), ".1f") if diag.get("temp_max_c") is not None else "—")
            row.append(_fmt(diag.get("temp_avg_c"), ".1f") if diag.get("temp_avg_c") is not None else "—")
            row.append(result.get("status", "FAIL"))
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
    for header, justify, min_width in _result_headers():
        sub.add_column(
            header=header, justify=justify,
            min_width=min_width, overflow="fold",
        )

    for row in _test_rows(disk_results, test_names):
        sub.add_row(*row[:-1], format_status(row[-1]))

    return sub


def build_results_table(disks, results, test_names, show_lat_p99=False):
    """Строит одну непрерывную таблицу для всех накопителей."""
    global SHOW_LAT_P99
    SHOW_LAT_P99 = show_lat_p99

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
