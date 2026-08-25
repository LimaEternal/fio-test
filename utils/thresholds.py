"""
Пороги PASS/FAIL: загрузка, валидация и выбор для конкретного диска.

Никаких формул — два декларативных JSON-файла в configs/:

  base_thresholds.json  — общие лояльные пороги по интерфейсу и поколению
                          линка (nvme: gen3/gen4/gen5…, sas, sata) плюс
                          отдельная секция hdd;
  disk_thresholds.json  — персональные пороги конкретных моделей дисков,
                          ключ — модель из lsblk (нормализованная).

Приоритет выбора порогов для диска:
  1) персональная запись по нормализованной модели — применяется ЦЕЛИКОМ
     (тесты, которых нет в записи, остаются без порога => FAIL);
  2) секция hdd (rotational == 1), независимо от интерфейса;
  3) строка интерфейс+поколение из base_thresholds.json: для NVMe поколение
     определяется из скорости PCIe-линка (sysfs) с клампингом к доступным
     строкам, для sas/sata/hdd берётся первая заданная строка;
  4) поколение определить не удалось -> нижняя строка интерфейса.
"""

import json
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
BASE_THRESHOLDS_PATH = CONFIG_DIR / "base_thresholds.json"
DISK_THRESHOLDS_PATH = CONFIG_DIR / "disk_thresholds.json"

# Известные id тестов (совпадают с секциями .fio-конфигов)
KNOWN_TESTS = ("seq_read", "seq_write", "rand_read", "rand_write")

_SRC_PERSONAL = "персональные (по модели)"

_GEN_KEY_RE = re.compile(r"^gen(\d+)$")


def normalize_model(model) -> str:
    """Нормализует строку модели: upper-case + схлопывание пробелов."""
    return re.sub(r"\s+", " ", str(model or "").strip()).upper()


def _load_json(path: Path) -> dict:
    """Читает JSON-файл, при ошибке чтения/парсинга поднимает ValueError."""
    try:
        with Path(path).open(encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"не удалось прочитать {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"битый JSON в {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: корневой элемент должен быть объектом")
    return data


def load_base_thresholds(path=None) -> dict:
    """Читает общий файл порогов -> {интерфейс: {строка: {тест: {порог}}}}."""
    return _load_json(Path(path) if path else BASE_THRESHOLDS_PATH)


def load_disk_thresholds(path=None) -> dict:
    """Читает персональные пороги -> {модель: {тест: {порог}}}. Может быть пустым."""
    return _load_json(Path(path) if path else DISK_THRESHOLDS_PATH)


def _validate_metric(test_id: str, tv, ctx: str) -> None:
    """Проверяет значение одного теста: {min_bw_mb|min_iops: число > 0}."""
    where = f"{ctx}: '{test_id}'"
    if not isinstance(tv, dict):
        raise ValueError(f"{where} — должен быть объектом")
    metric = "min_bw_mb" if "min_bw_mb" in tv else "min_iops"
    if metric not in tv:
        raise ValueError(f"{where} — нет ни min_bw_mb, ни min_iops")
    extra = set(tv) - {metric}
    if extra:
        raise ValueError(f"{where} — лишние ключи {sorted(extra)}")
    val = tv[metric]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{where} — {metric} должен быть числом")
    if val <= 0:
        raise ValueError(f"{where} — {metric} должен быть больше нуля")


def validate_base_thresholds(base: dict) -> None:
    """
    Валидирует общий файл порогов. При ошибке поднимает ValueError.

    Требования: каждый интерфейс (nvme/sas/sata, опционально hdd) содержит
    хотя бы одну строку; каждая строка — все четыре теста с корректными
    значениями (общий файл — страховка, он обязан быть полным).
    """
    known_ifaces = {"nvme", "sas", "sata", "hdd"}
    if not base:
        raise ValueError("файл пуст — нужен хотя бы один интерфейс")
    for iface, rows in base.items():
        if iface not in known_ifaces:
            raise ValueError(
                f"неизвестный интерфейс '{iface}' "
                f"(ожидается один из {sorted(known_ifaces)})"
            )
        if not isinstance(rows, dict) or not rows:
            raise ValueError(f"'{iface}' — должна быть хотя бы одна строка порогов")
        for row_key, row in rows.items():
            ctx = f"'{iface}'/'{row_key}'"
            if not isinstance(row, dict) or not row:
                raise ValueError(f"{ctx} — пустая строка порогов")
            missing = set(KNOWN_TESTS) - set(row)
            if missing:
                raise ValueError(
                    f"{ctx} — отсутствуют тесты {sorted(missing)} "
                    f"(в общем файле должны быть все 4)"
                )
            for tid in row:
                _validate_metric(tid, row[tid], ctx)


def validate_disk_thresholds(personal: dict) -> None:
    """
    Валидирует файл персональных порогов. При ошибке поднимает ValueError.

    Записи могут быть неполными (применяются целиком, недостающее — FAIL),
    но каждый указанный тест обязан быть известен и содержать корректный
    порог — опечатки ловим на старте, а не после прогона.
    """
    for model, entry in personal.items():
        ctx = f"модель '{model}'"
        if not normalize_model(model):
            raise ValueError(f"{ctx} — пустой ключ модели")
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"{ctx} — запись должна быть непустым объектом")
        for tid, tv in entry.items():
            if tid not in KNOWN_TESTS:
                raise ValueError(
                    f"{ctx}: неизвестный тест '{tid}' "
                    f"(известные: {list(KNOWN_TESTS)})"
                )
            _validate_metric(tid, tv, ctx)


def _first_row(rows: Dict[str, dict]) -> Tuple[Optional[str], dict]:
    """Первая заданная строка секции (порядок как в файле)."""
    for key, row in rows.items():
        return key, row
    return None, {}


def _nvme_row_key(rows: Dict[str, dict], gen: Optional[int]) -> Optional[str]:
    """
    Ключ строки NVMe под поколение PCIe gen.

    Берётся строка со старшим доступным поколением <= gen; если gen ниже
    всех или неизвестен — нижняя (самая лояльная) строка.
    """
    parsed = []
    for key in rows:
        m = _GEN_KEY_RE.match(key.strip().lower())
        if m:
            parsed.append((int(m.group(1)), key))
    if not parsed:
        return next(iter(rows), None)
    parsed.sort()
    chosen = parsed[0][1]
    for n, key in parsed:
        if gen is not None and n <= gen:
            chosen = key
    return chosen


def _row_label(iface: str, row_key: Optional[str]) -> str:
    if row_key and row_key.strip().lower() not in ("any", "default"):
        return f"общие ({iface} {row_key})"
    return f"общие ({iface})"


def resolve_thresholds(
    disk: dict, base: dict, personal: dict
) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """
    Итоговые пороги диска и источник каждого порога.

    Возвращает (thresholds, source):
      thresholds — {test_id: {min_bw_mb|min_iops}};
      source     — {test_id: "персональные (по модели)" | "общие (<строка>)"}.
    """
    model_key = normalize_model(disk.get("model"))
    entry = personal.get(model_key)
    if entry:
        return dict(entry), {tid: _SRC_PERSONAL for tid in entry}

    profile = disk.get("profile") or {}
    iface = (profile.get("interface") or disk.get("tran") or "").lower()

    if profile.get("rotational") == 1:
        rows = base.get("hdd")
        if isinstance(rows, dict) and rows:
            key, row = _first_row(rows)
            label = _row_label("hdd", key)
            return dict(row), {tid: label for tid in row}

    rows = base.get(iface)
    if not isinstance(rows, dict) or not rows:
        return {}, {}

    if iface == "nvme":
        gen = None
        link = profile.get("link") or {}
        speed = link.get("speed_gts")
        if speed:
            try:
                from utils.hw_profile import link_generation
                gen = link_generation(float(speed))
            except (TypeError, ValueError):
                gen = None
        key = _nvme_row_key(rows, gen)
    else:
        key, row = _first_row(rows)
        label = _row_label(iface, key)
        return dict(row), {tid: label for tid in row}

    row = rows.get(key) or {}
    label = _row_label(iface, key)
    return dict(row), {tid: label for tid in row}
