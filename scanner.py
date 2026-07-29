"""
Модуль определения несистемных дисков.

Парсит вывод lsblk в JSON-формате, фильтрует системные накопители
(с точкой монтирования /) и классифицирует оставшиеся по типу интерфейса.
"""

import json
import subprocess
from typing import Dict, List, Optional


def _detect_interface(disk_name: str, raw_tran: Optional[str]) -> str:
    """
    Определяет тип интерфейса диска.

    Если lsblk не вернул поле tran (бывает на старых ядрах),
    пытаемся угадать по имени устройства: nvme* → NVME, иначе → SATA.
    """
    tran = (raw_tran or "").upper()

    if not tran:
        return "NVME" if "nvme" in disk_name else "SATA"

    return tran


def get_non_system_disks(known_interfaces: Dict[str, list]) -> List[dict]:
    """
    Сканирует систему и возвращает список несистемных дисков.

    Параметры:
        known_interfaces — словарь {INTERFACE_NAME: config}, используется
                           для проверки, что интерфейс поддерживается.

    Возвращает список словарей:
        name, path, model, serial, tran, size, phy_sec
    """
    cmd = [
        "lsblk", "--json",
        "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINT,PHY-SEC",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout).get("blockdevices", [])

    disks = []

    for d in data:
        if d.get("type") != "disk":
            continue

        if d.get("size") in ("0B", "0"):
            continue

        has_root = d.get("mountpoint") == "/"
        if "children" in d:
            for child in d["children"]:
                if child.get("mountpoint") == "/":
                    has_root = True

        if has_root:
            continue

        tran = _detect_interface(d["name"], d.get("tran"))
        if tran not in known_interfaces:
            tran = "SATA"

        disks.append({
            "name": d["name"],
            "path": f"/dev/{d['name']}",
            "model": d.get("model") or "Unknown Model",
            "serial": d.get("serial") or "Unknown SN",
            "tran": tran,
            "size": d.get("size"),
            "phy_sec": int(d.get("phy-sec") or 512),
        })

    return disks
