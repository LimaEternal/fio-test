"""
Утилиты форматирования чисел в человекочитаемый вид для консоли и отчётов.
"""


def format_bytes(n: float) -> str:
    """Форматирует байты в человекочитаемый вид (Б/КБ/МБ/ГБ/ТБ)."""
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}ПБ"


def format_duration(sec: float) -> str:
    """Форматирует длительность в человекочитаемый вид (часы/минуты/секунды)."""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    if m:
        return f"{m} мин {s:02d} с"
    return f"{s} с"


def format_bw(bps: float) -> str:
    """Форматирует скорость (байт/с) в человекочитаемый вид (Б/с…ГБ/с)."""
    value = float(bps)
    if value <= 0:
        return "—"
    for unit in ("Б/с", "КБ/с", "МБ/с", "ГБ/с"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ/с"
