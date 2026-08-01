"""
Модуль определения несистемных дисков.

Парсит вывод lsblk в JSON-формате, фильтрует системные накопители
(с точкой монтирования / и другими системными путями на любой глубине вложенности)
и классифицирует оставшиеся по типу интерфейса.
"""

import json
import re
import subprocess
from pathlib import Path
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


def _is_system_mount(mp: str) -> bool:
    """
    Проверяет, является ли точка монтирования системной (критической для работы ОС).
    """
    if not mp:
        return False

    # Список критических системных директорий
    system_dirs = {
        "/", "/boot", "/boot/efi", "/usr", "/var", "/etc",
        "/home", "/opt", "/srv", "/root"
    }

    mp_clean = mp.strip()
    if mp_clean in system_dirs:
        return True

    # Защищаем все поддиректории системных путей (например, /var/log, /home/user)
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

    # Рекурсивно проверяем потомков (разделы -> LVM-тома -> и т.д.)
    if "children" in device:
        for child in device["children"]:
            if _is_system_device(child):
                return True

    return False


def get_nvme_pcie_info(disk_name: str) -> dict:
    """
    Определяет поколение PCIe и ширину шины (width) для NVMe диска на Linux.
    Возвращает словарь: {'gen': int|None, 'width': int|None, 'speed_gts': float|None}
    """
    info = {"gen": None, "width": None, "speed_gts": None}
    
    sys_path = Path(f"/sys/block/{disk_name}/device")
    if not sys_path.exists():
        sys_path = Path(f"/sys/class/block/{disk_name}/device")
        
    if not sys_path.exists():
        return info

    try:
        real_path = sys_path.resolve()
        current_dir = real_path
        speed_file = None
        width_file = None
        
        # Поднимаемся наверх до родительского PCI-устройства (до 5 уровней)
        for _ in range(5):
            if (current_dir / "current_link_speed").exists() and (current_dir / "current_link_width").exists():
                speed_file = current_dir / "current_link_speed"
                width_file = current_dir / "current_link_width"
                break
            current_dir = current_dir.parent
            if current_dir == current_dir.parent or current_dir.name in ("", "sys", "devices"):
                break
                
        if speed_file and width_file:
            speed_str = speed_file.read_text(encoding="utf-8").strip()
            width_str = width_file.read_text(encoding="utf-8").strip()
            
            speed_match = re.search(r"(\d+(?:\.\d+)?)\s*GT/s", speed_str)
            if speed_match:
                speed_gts = float(speed_match.group(1))
                info["speed_gts"] = speed_gts
                
                # Маппинг скоростей PCIe (GT/s -> Generation)
                if speed_gts >= 64.0:
                    info["gen"] = 6
                elif speed_gts >= 32.0:
                    info["gen"] = 5
                elif speed_gts >= 16.0:
                    info["gen"] = 4
                elif speed_gts >= 8.0:
                    info["gen"] = 3
                elif speed_gts >= 5.0:
                    info["gen"] = 2
                else:
                    info["gen"] = 1
                    
            if width_str.isdigit():
                info["width"] = int(width_str)
                
    except Exception:
        pass  # Беззвучно игнорируем ошибки на не-Linux системах
        
    return info


def get_non_system_disks(known_interfaces: Dict[str, list]) -> List[dict]:
    """
    Сканирует систему и возвращает список несистемных дисков.

    Параметры:
        known_interfaces — словарь {INTERFACE_NAME: config}, используется
                           для проверки, что интерфейс поддерживается.

    Возвращает список словарей:
        name, path, model, serial, tran, size, phy_sec, slot, pcie_info
    """
    cmd = [
        "lsblk", "--json",
        "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINT,PHY-SEC,HCTL",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            "Не удалось запустить утилиту 'lsblk'. Пожалуйста, убедитесь, "
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
            f"Не удалось распарсить вывод 'lsblk' as JSON. "
            f"Ошибка синтаксиса: {e}"
        )

    disks = []

    for d in data:
        if d.get("type") != "disk":
            continue

        if d.get("size") in ("0B", "0"):
            continue

        # Рекурсивная проверка: если диск или любой его потомок имеет системные разделы - пропускаем
        if _is_system_device(d):
            continue

        tran = _detect_interface(d["name"], d.get("tran"))
        if tran not in known_interfaces:
            tran = "SATA"

        slot = d.get("hctl") or ""
        if not slot and "nvme" in d["name"]:
            m = re.match(r"(nvme\d+)", d["name"])
            if m:
                slot = m.group(1)

        pcie_info = {"gen": None, "width": None, "speed_gts": None}
        if tran == "NVME":
            pcie_info = get_nvme_pcie_info(d["name"])

        disks.append({
            "name": d["name"],
            "path": f"/dev/{d['name']}",
            "model": d.get("model") or "Unknown Model",
            "serial": d.get("serial") or "Unknown SN",
            "tran": tran,
            "size": d.get("size"),
            "phy_sec": int(d.get("phy-sec") or 512),
            "slot": slot,
            "pcie_info": pcie_info,
        })

    return disks
