"""
Отбор и классификация блочных устройств для тестирования.

Сканирует вывод lsblk, фильтрует системные накопители (смонтированные
на системные пути) и классифицирует остальные на «занятые» (есть данные)
и «пустые/целевые» (тестируем только их). Профиль железа для каждого
диска берётся из utils.hw_profile.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.hw_profile import _detect_interface, collect_hw_profile, _get_numa_node


def _is_system_mount(mp: str) -> bool:
    """
    Проверяет, является ли точка монтирования системной (критической для работы ОС).
    """
    if not mp:
        return False

    system_dirs = {
        "/", "/boot", "/boot/efi", "/usr", "/var", "/etc",
        "/home", "/opt", "/srv", "/root",
    }

    mp_clean = mp.strip()
    if mp_clean in system_dirs:
        return True

    for s_dir in system_dirs:
        if s_dir != "/" and mp_clean.startswith(s_dir + "/"):
            return True

    return False


def _is_system_device(device: dict) -> bool:
    """
    Рекурсивно проверяет, является ли устройство или любой из его потомков системным.
    Проверяет как поле 'mountpoint', так и 'mountpoints' (для разных версий lsblk).
    """
    for key in ("mountpoint", "mountpoints"):
        val = device.get(key)
        if val:
            if isinstance(val, list):
                for mp in val:
                    if mp and _is_system_mount(str(mp)):
                        return True
            elif isinstance(val, str):
                if _is_system_mount(val):
                    return True

    if "children" in device:
        for child in device["children"]:
            if _is_system_device(child):
                return True

    return False


def _device_has_partitions(device: dict) -> bool:
    """Рекурсивно ищет таблицу разделов (child с type == 'part')."""
    for child in device.get("children", []):
        if child.get("type") == "part":
            return True
        if _device_has_partitions(child):
            return True
    return False


def _device_has_filesystem(device: dict) -> bool:
    """Рекурсивно ищет файловую систему (fstype задан)."""
    if device.get("fstype"):
        return True
    for child in device.get("children", []):
        if _device_has_filesystem(child):
            return True
    return False


def _device_is_mounted_anywhere(device: dict) -> bool:
    """Рекурсивно проверяет, смонтирован ли диск/потомок в любой путь."""
    for key in ("mountpoint", "mountpoints"):
        val = device.get(key)
        if val:
            if isinstance(val, list):
                if any(str(v).strip() for v in val):
                    return True
            elif isinstance(val, str) and val.strip():
                return True
    for child in device.get("children", []):
        if _device_is_mounted_anywhere(child):
            return True
    return False


def _is_occupied_device(device: dict) -> bool:
    """
    Диск «занят» (на нём есть данные) — его нельзя трогать по умолчанию:
    есть таблица разделов, ФС (fstype) или смонтирован в любой путь.
    """
    return (
        _device_has_partitions(device)
        or _device_has_filesystem(device)
        or _device_is_mounted_anywhere(device)
    )


def _find_root_mount_name(node: dict) -> Optional[str]:
    """Рекурсивно ищет имя устройства с корневой ФС (/). Возвращает имя или None."""
    mp = node.get("mountpoint")
    if mp == "/":
        return node.get("name", "?")
    for child in node.get("children", []):
        result = _find_root_mount_name(child)
        if result:
            return result
    return None


def scan_disks(known_interfaces: Dict[str, list]) -> Tuple[List[dict], List[dict], List[dict]]:
    """
    Сканирует систему и возвращает три списка: (system_disks, occupied_disks, target_disks).

    Системные диски   — хотя бы один потомок смонтирован на системный путь (/, /boot …).
    Занятые диски     — есть таблица разделов, ФС (fstype) или смонтированы в любой путь;
                        тестированию НЕ подлежат (на них есть данные).
    Целевые диски     — абсолютно пустые (нет разделов/ФС, не смонтированы);
                        единственные, которые скрипт реально тестирует.

    Возвращает:
        (system_disks, occupied_disks, target_disks)
    """
    cmd = [
        "lsblk", "--json",
        "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINT,FSTYPE,PHY-SEC,HCTL",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            "Не удалось запустить утилиту 'lsblk'. Убедитесь, "
            "что пакет 'util-linux' установлен в вашей системе."
        )
    except subprocess.CalledProcessError as e:
        stderr_msg = e.stderr.strip() if e.stderr else str(e)
        raise RuntimeError(
            f"Ошибка при выполнении команды 'lsblk' (код возврата {e.returncode}):\n{stderr_msg}"
        )

    try:
        data = json.loads(result.stdout).get("blockdevices", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Не удалось распарсить вывод 'lsblk' как JSON. "
            f"Ошибка синтаксиса: {e}"
        )

    system_disks = []
    occupied_disks = []
    target_disks = []

    for d in data:
        if d.get("type") != "disk":
            continue

        if d.get("size") in ("0B", "0"):
            continue

        is_system = _is_system_device(d)
        root_partition = _find_root_mount_name(d) if is_system else None

        tran = _detect_interface(d["name"], d.get("tran"))
        if tran not in known_interfaces:
            tran = "sata"

        slot = d.get("hctl") or ""
        if not slot and "nvme" in d["name"]:
            m = re.match(r"(nvme\d+)", d["name"])
            if m:
                slot = m.group(1)

        profile = collect_hw_profile(d["name"], tran)

        disk_info = {
            "name": d["name"],
            "path": f"/dev/{d['name']}",
            "model": d.get("model") or "Unknown Model",
            "serial": d.get("serial") or "Unknown SN",
            "tran": tran,
            "size": d.get("size"),
            "phy_sec": int(d.get("phy-sec") or 512),
            "slot": slot,
            "profile": profile,
            "root_partition": root_partition,
            "numa_node": _get_numa_node(d["name"]),
            "occupied": _is_occupied_device(d),
        }

        if is_system:
            system_disks.append(disk_info)
        elif disk_info["occupied"]:
            occupied_disks.append(disk_info)
        else:
            target_disks.append(disk_info)

    return system_disks, occupied_disks, target_disks
