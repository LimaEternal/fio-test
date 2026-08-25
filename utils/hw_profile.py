"""
Профилирование аппаратной части диска из sysfs.

Собирает физику линка (PCIe/SAS/SATA), параметры очереди блочного
устройства, MaxPayload PCIe и NUMA-узел; вычисляет теоретические
потолки шины (используются для подбора параметров тестов).
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional


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
        "gen": link_generation(cur_gts),
        "width": cur_width,
        "speed_gts": cur_gts,
        "max_gen": link_generation(max_gts) if max_gts else None,
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


def link_generation(speed_gts: float) -> int:
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


def nvme_line_rate_mbps(gts: float, width: int) -> float:
    """Базовая линейная скорость NVMe без учёта кодирования/TLP (МБ/с).

    (GT/s × 1000/8) × width — основа для link_bandwidth_mbps;
    вынесена, чтобы не дублировать формулу.
    """
    return gts * width * 1000.0 / 8.0


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
            return nvme_line_rate_mbps(gts, width) * enc

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


def _read_mpss_from_config(bdf: str) -> Optional[int]:
    """MaxPayload (байты) из PCI config space без lspci.

    Работает и под Intel VMD, где sysfs-атрибут mpss отсутствует.
    Читает /sys/bus/pci/devices/<bdf>/config, ищет PCIe Capability (ID 0x10),
    из DevCap (bits 2:0) декодирует MaxPayload: 128 << enc.
    """
    cfg = Path(f"/sys/bus/pci/devices/{bdf}/config")
    if not cfg.exists():
        return None
    try:
        data = cfg.read_bytes()
    except OSError:
        return None
    if len(data) < 0x40:
        return None
    cap = data[0x34]  # Capabilities Pointer
    for _ in range(64):  # защита от петли в списке возможностей
        if cap < 0x40 or cap + 8 > len(data):
            break
        if data[cap] == 0x10:  # PCI Express Capability
            devcap = int.from_bytes(data[cap + 4:cap + 8], "little")
            return 128 << (devcap & 0x7)
        cap = data[cap + 1]  # Next Capability Pointer
        if cap == 0:
            break
    return None


def _read_mpss(bdf: str) -> Optional[int]:
    """MaxPayload (байты) устройства: sysfs mpss, иначе PCI config space."""
    mpss = Path(f"/sys/bus/pci/devices/{bdf}/mpss")
    if mpss.exists():
        try:
            return int(mpss.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    return _read_mpss_from_config(bdf)


def _read_upstream_max_payload(bdf: str) -> Optional[int]:
    """Best-effort: MaxPayload upstream PCIe-моста (порта) из sysfs.

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
        return _read_mpss(bridge.name)
    except (OSError, ValueError):
        return None


def read_nvme_max_payload(disk_name: str) -> Optional[Dict[str, Optional[int]]]:
    """
    Читает MaxPayload (макс. размер TLP PCIe) NVMe-контроллера.

    MaxPayload — PCIe-уровень, относится к самому контроллеру диска
    (не к кабелю/протоколу SATA/SAS). Возвращает
    {'device': int_байты, 'port': int_байты|None}. При отсутствии
    данных в sysfs — None.
    """
    link_dir = find_nvme_link_dir(disk_name)
    if not link_dir:
        return None
    bdf = link_dir.name
    device = _read_mpss(bdf)
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
