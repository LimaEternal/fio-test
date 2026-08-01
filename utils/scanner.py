"""
Модуль определения несистемных дисков.

Парсит вывод lsblk в JSON-формате, рекурсивно обходит дерево блочных
устройств, фильтрует системные накопители (с точкой монтирования /)
и классифицирует оставшиеся по типу интерфейса.
"""

import json
import re
import subprocess
from typing import Dict, List, Optional, Tuple


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


def _collect_mountpoints(node: dict) -> List[str]:
    """Рекурсивно собирает все mountpoints из дерева потомков."""
    mounts = []
    mp = node.get("mountpoint")
    if mp:
        mounts.append(mp)
    for child in node.get("children", []):
        mounts.extend(_collect_mountpoints(child))
    return mounts


def _find_root_mount(node: dict) -> Optional[str]:
    """Рекурсивно ищет корневую ФС (/) среди потомков. Возвращает путь к LV/ partition или None."""
    mp = node.get("mountpoint")
    if mp == "/":
        name = node.get("name", "?")
        return name
    for child in node.get("children", []):
        result = _find_root_mount(child)
        if result:
            return result
    return None


def scan_disks(known_interfaces: Dict[str, list]) -> Tuple[List[dict], List[dict]]:
    """
    Сканирует систему и возвращает два списка: (system_disks, target_disks).

    Системные диски — те, у которых хотя бы один потомок смонтирован на /.
    Целевые диски — все остальные несистемные диски.

    Возвращает:
        (system_disks, target_disks) — кортеж из двух списков словарей:
        name, path, model, serial, tran, size, phy_sec, slot, root_partition
    """
    cmd = [
        "lsblk", "--json",
        "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINT,PHY-SEC,HCTL",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout).get("blockdevices", [])

    system_disks = []
    target_disks = []

    for d in data:
        if d.get("type") != "disk":
            continue

        if d.get("size") in ("0B", "0"):
            continue

        root_partition = _find_root_mount(d)

        tran = _detect_interface(d["name"], d.get("tran"))
        if tran not in known_interfaces:
            tran = "SATA"

        slot = d.get("hctl") or ""
        if not slot and "nvme" in d["name"]:
            m = re.match(r"(nvme\d+)", d["name"])
            if m:
                slot = m.group(1)

        disk_info = {
            "name": d["name"],
            "path": f"/dev/{d['name']}",
            "model": d.get("model") or "Unknown Model",
            "serial": d.get("serial") or "Unknown SN",
            "tran": tran,
            "size": d.get("size"),
            "phy_sec": int(d.get("phy-sec") or 512),
            "slot": slot,
            "root_partition": root_partition,
        }

        if root_partition:
            system_disks.append(disk_info)
        else:
            target_disks.append(disk_info)

    return system_disks, target_disks


def get_non_system_disks(known_interfaces: Dict[str, list]) -> List[dict]:
    """
    Обратная совместимость: возвращает только целевые диски.
    """
    _, target_disks = scan_disks(known_interfaces)
    return target_disks
