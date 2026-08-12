"""
Модуль определения несистемных дисков.

Парсит вывод lsblk в JSON-формате, рекурсивно обходит дерево блочных
устройств, фильтрует системные накопители (с точкой монтирования /
и другими системными путями на любой глубине вложенности) и классифицирует
оставшиеся по типу интерфейса.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _read_queue_file(path: Path, default: int) -> int:
    """Читает целое число из sysfs-файла, возвращает default при ошибке."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default


def _read_queue_info(disk_name: str) -> Dict[str, int]:
    """Читает информацию о блоках из /sys/block/<d>/queue/."""
    queue_dir = Path(f"/sys/block/{disk_name}/queue")
    return {
        "logical_block_size": _read_queue_file(queue_dir / "logical_block_size", 512),
        "physical_block_size": _read_queue_file(queue_dir / "physical_block_size", 4096),
        "minimum_io_size": _read_queue_file(queue_dir / "minimum_io_size", 512),
        "optimal_io_size": _read_queue_file(queue_dir / "optimal_io_size", 0),
        "rotational": _read_queue_file(queue_dir / "rotational", 0),
    }


def _read_nvme_link(disk_name: str) -> Optional[Dict]:
    """Читает текущий и максимальный линк PCIe для NVMe."""
    link_dir = find_nvme_link_dir(disk_name)
    if not link_dir:
        return None

    try:
        speed_cur = (link_dir / "current_link_speed").read_text(encoding="utf-8").strip()
        width_cur = (link_dir / "current_link_width").read_text(encoding="utf-8").strip()
        speed_max = (link_dir / "max_link_speed").read_text(encoding="utf-8").strip()
        width_max = (link_dir / "max_link_width").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    def parse_speed(s: str) -> Optional[float]:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        return float(m.group(1)) if m else None

    def parse_width(w: str) -> Optional[int]:
        m = re.search(r"(\d+)", w)
        return int(m.group(1)) if m else None

    cur_gts = parse_speed(speed_cur)
    cur_width = parse_width(width_cur)
    max_gts = parse_speed(speed_max)
    max_width = parse_width(width_max)

    if cur_gts is None or cur_width is None:
        return None

    return {
        "gen": _link_generation(cur_gts),
        "width": cur_width,
        "speed_gts": cur_gts,
        "max_gen": _link_generation(max_gts) if max_gts else None,
        "max_width": max_width,
        "max_speed_gts": max_gts,
        "source": "sysfs",
    }


def _read_sas_link(disk_name: str) -> Optional[Dict]:
    """Читает negotiated/max linkrate для SAS диска через sas_address."""
    try:
        scsi_dev = Path(f"/sys/block/{disk_name}/device")
        if not scsi_dev.exists():
            return None

        dev_path = scsi_dev.resolve()
        sas_addr_file = dev_path / "sas_address"
        if not sas_addr_file.exists():
            return None

        sas_address = sas_addr_file.read_text(encoding="utf-8").strip()
        if not sas_address:
            return None

        for phy_dir in Path("/sys/class/sas_phy").iterdir():
            if not phy_dir.is_dir():
                continue
            addr_file = phy_dir / "sas_address"
            if not addr_file.exists():
                continue
            if addr_file.read_text(encoding="utf-8").strip() != sas_address:
                continue

            neg_file = phy_dir / "negotiated_linkrate"
            max_file = phy_dir / "maximum_linkrate"
            if not neg_file.exists() or not max_file.exists():
                continue

            neg_str = neg_file.read_text(encoding="utf-8").strip()
            max_str = max_file.read_text(encoding="utf-8").strip()

            def parse_gbps(s: str) -> Optional[float]:
                m = re.search(r"(\d+(?:\.\d+)?)", s)
                return float(m.group(1)) if m else None

            return {
                "negotiated_gbps": parse_gbps(neg_str),
                "maximum_gbps": parse_gbps(max_str),
                "source": "sas_phy",
            }
    except OSError:
        pass
    return None


def _read_sata_link(disk_name: str) -> Optional[Dict]:
    """Читает sata_spd_limit для SATA диска через ata_link."""
    try:
        dev_path = Path(f"/sys/block/{disk_name}").resolve()
        path_str = str(dev_path)

        link_match = re.search(r"/link(\d+)\.(\d+)", path_str)
        if not link_match:
            return None

        link_num = link_match.group(1)
        link_port = link_match.group(2)
        link_dir = Path(f"/sys/class/ata_link/link{link_num}.{link_port}")

        if not link_dir.exists():
            return None

        spd_file = link_dir / "sata_spd_limit"
        hw_spd_file = link_dir / "hw_sata_spd_limit"

        if not spd_file.exists():
            return None

        def parse_gbps(s: str) -> Optional[float]:
            m = re.search(r"(\d+(?:\.\d+)?)", s)
            return float(m.group(1)) if m else None

        spd_str = spd_file.read_text(encoding="utf-8").strip()
        hw_spd_str = hw_spd_file.read_text(encoding="utf-8").strip() if hw_spd_file.exists() else ""

        return {
            "spd_limit_gbps": parse_gbps(spd_str),
            "hw_spd_limit_gbps": parse_gbps(hw_spd_str) if hw_spd_str else None,
            "source": "ata_link",
        }
    except OSError:
        pass
    return None


def _link_generation(speed_gts: float) -> int:
    """Сопоставляет скорость линка (GT/s) с поколением PCIe."""
    if speed_gts >= 128.0:
        return 7
    if speed_gts >= 64.0:
        return 6
    if speed_gts >= 32.0:
        return 5
    if speed_gts >= 16.0:
        return 4
    if speed_gts >= 8.0:
        return 3
    if speed_gts >= 5.0:
        return 2
    return 1


def _detect_interface(disk_name: str, raw_tran: Optional[str]) -> str:
    """
    Определяет тип интерфейса диска.

    Имя устройства (nvme*) имеет приоритет — на системах с Intel VMD
    lsblk может отдавать в поле tran нестандартные значения (например,
    "pcie"), при этом диск остаётся NVMe. Если tran известен (sas/sata),
    используем его, иначе считаем диск SATA.
    """
    if "nvme" in disk_name:
        return "nvme"

    tran = (raw_tran or "").lower()

    if tran in ("sas", "sata"):
        return tran

    return "sata"


def collect_hw_profile(disk_name: str, tran: str) -> Dict:
    """
    Собирает полный профиль железа диска из sysfs.

    Возвращает dict с ключами:
      - interface: "nvme"/"sas"/"sata"
      - logical_block_size, physical_block_size, minimum_io_size, optimal_io_size
      - rotational: 0=SSD, 1=HDD
      - link: dict с информацией о линке (см. ниже)
      - ceiling_mbps: оценочный потолок скорости в МБ/с

    link для NVMe:
      {"gen": int, "width": int, "speed_gts": float,
       "max_gen": int, "max_width": int, "max_speed_gts": float, "source": "sysfs"}
    link для SAS:
      {"negotiated_gbps": float, "maximum_gbps": float, "source": "sas_phy"}
    link для SATA:
      {"spd_limit_gbps": float, "hw_spd_limit_gbps": float, "source": "ata_link"}
    """
    queue_info = _read_queue_info(disk_name)
    interface = tran

    if interface == "nvme":
        link = _read_nvme_link(disk_name)
    elif interface == "sas":
        link = _read_sas_link(disk_name)
    else:  # sata
        link = _read_sata_link(disk_name)

    ceiling = estimate_ceiling_mbps(interface, link, queue_info["rotational"])

    return {
        "interface": interface,
        "logical_block_size": queue_info["logical_block_size"],
        "physical_block_size": queue_info["physical_block_size"],
        "minimum_io_size": queue_info["minimum_io_size"],
        "optimal_io_size": queue_info["optimal_io_size"],
        "rotational": queue_info["rotational"],
        "link": link,
        "ceiling_mbps": ceiling,
    }


def estimate_ceiling_mbps(interface: str, link: Optional[Dict], rotational: int) -> float:
    """
    Оценивает максимальную реальную скорость диска в МБ/с.

    NVMe: потолок по gen×width (приближённо по теоретической пропускной способности PCIe)
    SAS: negotiated_gbps × 0.85 (учёт накладных расходов)
    SATA: SSD ≈ 550 МБ/с, HDD ≈ 250 МБ/с
    """
    if interface == "nvme" and link:
        gen = link.get("gen")
        width = link.get("width")
        if gen and width:
            # Приблизительные значения по поколениям PCIe (x4)
            # Gen3=3.94 GB/s, Gen4=7.88 GB/s, Gen5=15.75 GB/s
            gen_speeds = {3: 4000, 4: 8000, 5: 16000, 6: 32000, 7: 64000}
            base = gen_speeds.get(gen, 4000)
            return base * width // 4

    if interface == "sas" and link:
        neg_gbps = link.get("negotiated_gbps")
        if neg_gbps:
            return neg_gbps * 1000 * 0.85

    if interface == "sata":
        if rotational == 1:  # HDD
            return 250.0
        else:  # SSD
            return 550.0

    return 0.0


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


def _link_generation(speed_gts: float) -> int:
    """Сопоставляет скорость линка (GT/s) с поколением PCIe."""
    if speed_gts >= 64.0:
        return 6
    if speed_gts >= 32.0:
        return 5
    if speed_gts >= 16.0:
        return 4
    if speed_gts >= 8.0:
        return 3
    if speed_gts >= 5.0:
        return 2
    return 1


def _find_link_files(start_dir: Path):
    """Ищет current_link_speed/current_link_width, поднимаясь от start_dir вверх.

    Возвращает (speed_file, width_file) или None. Лимит в 8 уровней
    защищает от бесконечного подъёма к корню ФС.
    """
    current_dir = start_dir
    for _ in range(8):
        speed_file = current_dir / "current_link_speed"
        width_file = current_dir / "current_link_width"
        if speed_file.exists() and width_file.exists():
            return speed_file, width_file
        parent = current_dir.parent
        if parent == current_dir:
            break
        current_dir = parent
    return None


def find_nvme_link_dir(disk_name: str) -> Optional[Path]:
    """
    Находит каталог с current_link_speed/current_link_width для NVMe диска.

    Под Intel VMD /sys/block/<name>/device ведёт в виртуальный каталог
    nvme-subsystem без линк-файлов, поэтому первым пробуется реальная
    PCI-функция /sys/class/nvme/<nvmeN>/device.

    Возвращает Path к каталогу с линк-файлами или None.
    """
    nvme_match = re.match(r"(nvme\d+)", disk_name)
    nvme_dev = nvme_match.group(1) if nvme_match else None

    start_dirs = []
    if nvme_dev:
        start_dirs.append(Path(f"/sys/class/nvme/{nvme_dev}/device"))
    start_dirs.append(Path(f"/sys/block/{disk_name}/device"))
    start_dirs.append(Path(f"/sys/class/block/{disk_name}/device"))

    for start in start_dirs:
        if not start.exists():
            continue
        try:
            real_path = start.resolve()
        except Exception:
            continue
        try:
            found = _find_link_files(real_path)
        except Exception:
            continue
        if found:
            speed_file, _ = found
            return speed_file.parent
    return None


def get_nvme_pcie_info(disk_name: str) -> dict:
    """
    Определяет поколение PCIe и ширину шины (width) для NVMe диска на Linux.
    Возвращает словарь: {'gen': int|None, 'width': int|None, 'speed_gts': float|None}
    """
    info = {"gen": None, "width": None, "speed_gts": None}

    link_dir = find_nvme_link_dir(disk_name)
    if not link_dir:
        return info

    try:
        speed_str = (link_dir / "current_link_speed").read_text(encoding="utf-8").strip()
        width_str = (link_dir / "current_link_width").read_text(encoding="utf-8").strip()

        speed_match = re.search(r"(\d+(?:\.\d+)?)\s*GT/s", speed_str)
        if speed_match:
            speed_gts = float(speed_match.group(1))
            info["speed_gts"] = speed_gts
            info["gen"] = _link_generation(speed_gts)

        if width_str.isdigit():
            info["width"] = int(width_str)
    except Exception:
        pass

    return info


def _get_numa_node(disk_name: str) -> Optional[int]:
    """
    Определяет NUMA-узел, на котором находится диск.

    Возвращает номер NUMA-узла или None.
    """
    try:
        numa_path = Path(f"/sys/class/block/{disk_name}/device/numa_node")
        if numa_path.exists():
            numa_node = int(numa_path.read_text().strip())
            if numa_node >= 0:
                return numa_node
    except Exception:
        pass
    return None


def scan_disks(known_interfaces: Dict[str, list]) -> Tuple[List[dict], List[dict]]:
    """
    Сканирует систему и возвращает два списка: (system_disks, target_disks).

    Системные диски — те, у которых хотя бы один потомок смонтирован на системный путь.
    Целевые диски — все остальные несистемные диски.

    Возвращает:
        (system_disks, target_disks)
    """
    cmd = [
        "lsblk", "--json",
        "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINT,PHY-SEC,HCTL",
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

        pcie_info = {"gen": None, "width": None, "speed_gts": None}
        if tran == "nvme":
            pcie_info = get_nvme_pcie_info(d["name"])

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
            "pcie_info": pcie_info,
            "profile": profile,
            "root_partition": root_partition,
            "numa_node": _get_numa_node(d["name"]),
        }

        if is_system:
            system_disks.append(disk_info)
        else:
            target_disks.append(disk_info)

    return system_disks, target_disks


def get_non_system_disks(known_interfaces: Dict[str, list]) -> List[dict]:
    """Обратная совместимость: возвращает только целевые диски."""
    _, target_disks = scan_disks(known_interfaces)
    return target_disks
