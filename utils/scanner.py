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
        "max_hw_sectors_kb": _read_queue_file(queue_dir / "max_hw_sectors_kb", 4096),
        "max_sectors_kb": _read_queue_file(queue_dir / "max_sectors_kb", 4096),
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
      - max_hw_sectors_kb, max_sectors_kb: лимиты размера одного I/O (sysfs)
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

    if interface == "nvme" and link is not None:
        link["max_payload"] = read_nvme_max_payload(disk_name)

    ceiling = estimate_ceiling_mbps(interface, link, queue_info["rotational"])

    return {
        "interface": interface,
        "logical_block_size": queue_info["logical_block_size"],
        "physical_block_size": queue_info["physical_block_size"],
        "minimum_io_size": queue_info["minimum_io_size"],
        "optimal_io_size": queue_info["optimal_io_size"],
        "max_hw_sectors_kb": queue_info["max_hw_sectors_kb"],
        "max_sectors_kb": queue_info["max_sectors_kb"],
        "rotational": queue_info["rotational"],
        "link": link,
        "ceiling_mbps": ceiling,
    }


# Коэффициенты кодирования линии: Gen3+ и PAM4 (Gen6/7) используют
# 128b/130b, Gen1/2, SAS и SATA — 8b/10b.
_ENC_128B130B = 0.9846
_ENC_8B10B = 0.8


def link_bandwidth_mbps(interface: str, link: Optional[Dict]) -> Optional[float]:
    """
    Теоретическая пропускная способность шины в МБ/с (без поправок на диск).

    Считается напрямую из физики линка (без таблицы поколений):
      NVMe: current_link_speed (GT/s) × width × кодирование;
      SAS:  negotiated_linkrate (Gbps) — 8b/10b;
      SATA: sata_spd_limit (Gbps) — 8b/10b.
    Возвращает None, если данные линка отсутствуют.
    """
    if interface == "nvme" and link:
        gts = link.get("speed_gts")
        width = link.get("width")
        if gts and width:
            enc = _ENC_128B130B if gts >= 8.0 else _ENC_8B10B
            return gts * width * 1000 / 8 * enc

    if interface == "sas" and link:
        neg_gbps = link.get("negotiated_gbps")
        if neg_gbps:
            # 8b/10b: 12 Gbps → 1200 МБ/с (Gbps / 10 × 1000)
            return neg_gbps * 100

    if interface == "sata":
        spd_gbps = (link or {}).get("spd_limit_gbps")
        if spd_gbps:
            return spd_gbps * 100

    return None


def estimate_ceiling_mbps(interface: str, link: Optional[Dict], rotational: int) -> float:
    """
    Оценивает максимальную реальную скорость диска в МБ/с.

    Для NVMe/SAS совпадает с пропускной способностью шины. Для SATA
    дополнительно ограничивается реальным потолком флеш/механики.
    """
    bw = link_bandwidth_mbps(interface, link)
    if interface == "sata":
        real_world = 250.0 if rotational == 1 else 550.0
        if bw is None:
            return real_world
        return min(bw, real_world)
    return bw or 0.0


# === Динамические пороги PASS/FAIL (Zero-Config, ТЗ) =========================
# TARGET_PERCENT — базовый уровень PASS относительно теоретического потолка
# шины/носителя. Задаётся константой или аргументом CLI --target-percent.
DEFAULT_TARGET_PERCENT = 0.90

# Эффективность TLP для NVMe: Gen4/Gen5+ (>=16 GT/s) держим 0.88,
# Gen3 (8 GT/s) — 0.87. Умножается на кодирование 128b/130b.
_NVME_TLP_EFF_GEN4PLUS = 0.88
_NVME_TLP_EFF_GEN3 = 0.87
# Доля записи NVMe от потолка шины (флеш-параллелизм/надёжность).
_NVME_WRITE_FACTOR = 0.53
# Доля записи SATA/SAS SSD от потолка шины (флеш/параллелизм).
_SATA_SAS_WRITE_FACTOR = 0.90
# Медиа-потолок HDD для последовательных операций (МБ/с).
_HDD_MEDIA_MBPS = 220.0
# Базовые потолки шин SATA III (6 Гбит/с) и SAS 12G (Гбит/с).
_SATA_BUS_MBPS_PER_GBPS = 550.0 / 6.0
_SAS_BUS_MBPS_PER_GBPS = 1150.0 / 12.0


def compute_pass_thresholds(disk: Dict, target_percent: float = DEFAULT_TARGET_PERCENT) -> Dict:
    """
    Динамические пороги PASS/FAIL для последовательных тестов (МБ/с),
    рассчитанные из профиля железа (sysfs) по ТЗ Zero-Config.

    Возвращает {"seq_read": {"min_bw_mb": float},
                 "seq_write": {"min_bw_mb": float}} или {} если данных
    sysfs недостаточно (тогда используется fallback из configs/thresholds.json).

    Категории:
      1) HDD (rotational == 1): BW_media = 220 МБ/с;
         read = write = BW_media * target_percent.
      2) NVMe (PCIe): BW_bus = (gts*1000/8)*(128/130)*tlp*width,
         tlp = 0.88 (>=16 GT/s) иначе 0.87;
         read = BW_bus * target_percent;
         write = BW_bus * 0.53 * target_percent.
      3) SATA/SAS SSD (rotational == 0, не PCIe):
         SATA bus = spd_limit_gbps * (550/6); SAS bus = neg_gbps * (1150/12);
         read = bus * target_percent; write = bus * 0.90 * target_percent.
    """
    profile = disk.get("profile") or {}
    interface = (profile.get("interface") or disk.get("tran") or "").lower()
    rotational = profile.get("rotational")
    link = profile.get("link")

    # Категория 1: HDD — механика, порог не зависит от линка.
    if rotational == 1:
        val = round(_HDD_MEDIA_MBPS * target_percent, 1)
        return {
            "seq_read": {"min_bw_mb": val},
            "seq_write": {"min_bw_mb": val},
        }

    # Категория 2: NVMe (PCIe).
    if interface == "nvme":
        if not link or not link.get("speed_gts") or not link.get("width"):
            return {}
        gts = float(link["speed_gts"])
        width = int(link["width"])
        tlp = _NVME_TLP_EFF_GEN4PLUS if gts >= 16.0 else _NVME_TLP_EFF_GEN3
        bw_bus = (gts * 1000.0 / 8.0) * (128.0 / 130.0) * tlp * width
        return {
            "seq_read": {"min_bw_mb": round(bw_bus * target_percent, 1)},
            "seq_write": {"min_bw_mb": round(bw_bus * _NVME_WRITE_FACTOR * target_percent, 1)},
        }

    # Категория 3: SATA/SAS SSD.
    if not link:
        return {}
    if interface == "sata":
        spd = link.get("spd_limit_gbps") or link.get("hw_spd_limit_gbps") or 0
        bus = float(spd) * _SATA_BUS_MBPS_PER_GBPS
    else:  # sas
        spd = link.get("negotiated_gbps") or link.get("maximum_gbps") or 0
        bus = float(spd) * _SAS_BUS_MBPS_PER_GBPS
    if bus <= 0:
        return {}
    return {
        "seq_read": {"min_bw_mb": round(bus * target_percent, 1)},
        "seq_write": {"min_bw_mb": round(bus * _SATA_SAS_WRITE_FACTOR * target_percent, 1)},
    }


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


def _parse_max_payload(text: str) -> Optional[int]:
    """Извлекает MaxPayload (байты) из вывода 'lspci -vvv' (поле DevCap)."""
    m = re.search(r"MaxPayload\s+(\d+)\s*bytes", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _read_upstream_max_payload(bdf: str) -> Optional[int]:
    """Best-effort: читает MaxPayload upstream PCIe-моста (порта) по BDF.

    Позволяет сравнить MaxPayload устройства с лимитом порта. При
    любой ошибке возвращает None (признак «не удалось проверить»).
    """
    try:
        dev_path = Path(f"/sys/bus/pci/devices/{bdf}")
        if not dev_path.exists():
            return None
        bridge = dev_path.resolve().parent
        if bridge.name == bdf:
            return None
        out = subprocess.run(
            ["lspci", "-vvv", "-s", bridge.name],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return _parse_max_payload(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def read_nvme_max_payload(disk_name: str) -> Optional[Dict[str, Optional[int]]]:
    """
    Читает MaxPayload (макс. размер TLP PCIe) NVMe-контроллера.

    MaxPayload — PCIe-уровень, относится к самому контроллеру диска
    (не к кабелю/протоколу SATA/SAS). Возвращает
    {'device': int_байты, 'port': int_байты|None}. При отсутствии
    данных или lspci — None.
    """
    link_dir = find_nvme_link_dir(disk_name)
    if not link_dir:
        return None
    bdf = link_dir.name
    try:
        out = subprocess.run(
            ["lspci", "-vvv", "-s", bdf],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    device = _parse_max_payload(out)
    if device is None:
        return None
    return {"device": device, "port": _read_upstream_max_payload(bdf)}


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
            "occupied": _is_occupied_device(d),
        }

        if is_system:
            system_disks.append(disk_info)
        elif disk_info["occupied"]:
            occupied_disks.append(disk_info)
        else:
            target_disks.append(disk_info)

    return system_disks, occupied_disks, target_disks


def get_non_system_disks(known_interfaces: Dict[str, list]) -> List[dict]:
    """Обратная совместимость: возвращает только целевые (пустые) диски."""
    _, _, target_disks = scan_disks(known_interfaces)
    return target_disks
