"""Парсер FIO-конфигов (.fio) в плоские списки аргументов fio.

Поддерживает подмножество формата fio, которое генерирует этот проект:
- секции [имя];
- строки key=value;
- голые булевы опции (например, stonewall) — эквивалент key=1;
- полнострочные комментарии ; и # (пустые строки игнорируются);
- секция [global] применяется ко всем остальным секциям: её опции идут
  первыми, а собственные опции секции могут их переопределить.

Результат: {id_секции: ["--key=value", ...]} в порядке следования секций.
Запуск fio не требуется — файл разбирается чистым Python.
"""

import re
from pathlib import Path

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
OPTION_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*)$")
BARE_OPTION_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*$")

GLOBAL_SECTION = "global"


class FioConfigError(ValueError):
    """Ошибка разбора .fio-файла."""


def parse_fio_jobfile(path):
    """Читает .fio-файл и возвращает {id_секции: [аргументы fio]}.

    Порядок секций сохраняется. Секция [global] в результат не попадает,
    а раскрывается в начале списка аргументов каждой секции.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FioConfigError(f"Не удалось прочитать {path}: {exc}") from exc

    raw = {}      # секция -> [(key, value), ...] в порядке следования
    order = []    # секции в порядке следования (без global)
    current = None

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue

        m = SECTION_RE.match(line)
        if m:
            current = m.group(1)
            if current not in raw:
                raw[current] = []
                if current != GLOBAL_SECTION:
                    order.append(current)
            continue

        m = OPTION_RE.match(line)
        if m and current is not None:
            raw[current].append((m.group(1), m.group(2).strip()))
            continue

        m = BARE_OPTION_RE.match(line)
        if m and current is not None:
            raw[current].append((m.group(1), "1"))
            continue

        raise FioConfigError(f"{path}:{lineno}: не удалось разобрать строку: {line!r}")

    global_opts = raw.get(GLOBAL_SECTION, [])
    result = {}
    for section in order:
        args = [f"--{key}={value}" for key, value in global_opts]
        args += [f"--{key}={value}" for key, value in raw[section]]
        result[section] = args

    if not result:
        raise FioConfigError(f"{path}: нет ни одной секции теста (кроме global)")
    return result
