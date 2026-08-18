import sys
from typing import Iterable, List

# Служебные ключи в results[disk_idx], не содержащие статусов тестов.
_PRIVATE_PREFIX = "_"


def _normalize(status) -> str:
    return str(status or "").strip().upper()


def extract_statuses(results) -> List[str]:
    """Извлекает статусы PASS/FAIL из структуры results fio-test.py.

    results — список словарей по дискам:
        results[disk_idx] = {
            test_id: {"status": "PASS"/"FAIL", ...},
            "_thresholds": {...},   # служебное, игнорируется
            "_wall_s": ...,         # служебное, игнорируется
        }
    Служебные ключи, начинающиеся с '_', пропускаются.

    Также принимает плоский список элементов, у каждого из которых есть
    status (dict-ключ или атрибут объекта).
    """
    statuses: List[str] = []
    if isinstance(results, dict):
        results = [results]
    for disk in results:
        if isinstance(disk, dict):
            # Плоский список элементов-результатов: {"status": "PASS", ...}
            if "status" in disk and not isinstance(disk.get("status"), dict):
                statuses.append(_normalize(disk.get("status")))
                continue
            for key, val in disk.items():
                if key.startswith(_PRIVATE_PREFIX):
                    continue
                if isinstance(val, dict):
                    statuses.append(_normalize(val.get("status")))
        else:
            statuses.append(_normalize(getattr(disk, "status", None)))
    return [s for s in statuses if s in ("PASS", "FAIL")]


def count_statuses(results) -> tuple:
    """Возвращает (fails, total) по извлечённым статусам PASS/FAIL."""
    fails = 0
    total = 0
    for s in extract_statuses(results):
        total += 1
        if s == "FAIL":
            fails += 1
    return fails, total


def decide_exit_code(results) -> int:
    """Решает итоговый код завершения по результатам тестов.

    results — структура results fio-test.py (список словарей по дискам)
    либо плоский список статусов/элементов с полем status.

    Возвращает:
        0 — все диски и все тесты PASS (fails == 0);
        1 — все тесты FAIL (fails == total);
        2 — есть хотя бы один FAIL, но не все (0 < fails < total).
    Пустой набор результатов трактуется как PASS (код 0).
    """
    fails, total = count_statuses(results)
    if total == 0:
        return 0
    if fails == 0:
        return 0
    if fails == total:
        return 1
    return 2


def sys_exit(results) -> None:
    """Завершает процесс с кодом, вычисленным decide_exit_code(results)."""
    sys.exit(decide_exit_code(results))
