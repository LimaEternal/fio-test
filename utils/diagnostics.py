"""
Модуль диагностики производительности дисков.

Во время fio-теста в отдельном потоке сэмплирует состояние системы —
только sysfs и /proc, без внешних зависимостей:
    * состояние PCIe-линка (current_link_speed/current_link_width) — просадка
      поколения или ширины под нагрузкой;
    * температура контроллера NVMe (hwmon temp1_input) — перегрев/троттлинг;
    * реальная нагрузка на диск из /proc/diskstats — фактические IOPS/МБ/с
      и длина очереди.

Также собирает статичную информацию: NUMA-нода диска и привязка CPU.
"""

import re
import threading
import time
from pathlib import Path
from typing import Optional

from utils.scanner import find_nvme_link_dir


def _nvme_dev_name(disk_name: str) -> Optional[str]:
    m = re.match(r"(nvme\d+)", disk_name)
    return m.group(1) if m else None


def collect_static_info(disk: dict) -> dict:
    """Собирает статичную диагностику диска: NUMA-нода и привязка CPU."""
    info = {"numa_node": None, "cpu_affinity": None}

    nvme_dev = _nvme_dev_name(disk.get("name", ""))
    if nvme_dev:
        numa_file = Path(f"/sys/class/nvme/{nvme_dev}/device/numa_node")
        if numa_file.exists():
            try:
                info["numa_node"] = numa_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except Exception:
        return info

    for line in status.splitlines():
        if line.startswith("Cpus_allowed_list"):
            info["cpu_affinity"] = line.split(":", 1)[1].strip()
            break
    return info


class DiagnosticSampler:
    """Сэмплирует линк, температуру и нагрузку на диск в отдельном потоке."""

    SECTOR_SIZE = 512

    def __init__(self, disk: dict, interval: float = 1.0):
        self.disk = disk
        self.interval = interval
        self.name = disk.get("name", "")
        self.nvme_dev = _nvme_dev_name(self.name)
        self.link_dir = find_nvme_link_dir(self.name)
        self._prev_diskstats = None
        self._prev_ts = None
        self.samples = []

    # --- чтение источников ---

    def _read_link(self):
        """Возвращает (gts, width) текущего линка PCIe или (None, None)."""
        if not self.link_dir:
            return None, None
        try:
            speed_str = (self.link_dir / "current_link_speed").read_text(encoding="utf-8").strip()
            width_str = (self.link_dir / "current_link_width").read_text(encoding="utf-8").strip()
        except Exception:
            return None, None

        speed_m = re.search(r"(\d+(?:\.\d+)?)", speed_str)
        width_m = re.search(r"(\d+)", width_str)
        gts = float(speed_m.group(1)) if speed_m else None
        width = int(width_m.group(1)) if width_m else None
        return gts, width

    def _read_temp(self) -> Optional[float]:
        """Возвращает температуру контроллера NVMe в °C или None."""
        if not self.nvme_dev:
            return None
        try:
            hwmon = Path(f"/sys/class/nvme/{self.nvme_dev}/hwmon")
            for temp_file in sorted(hwmon.glob("hwmon*/temp1_input")):
                value = int(temp_file.read_text(encoding="utf-8").strip())
                return value / 1000.0
        except Exception:
            pass
        return None

    def _read_diskstats(self) -> Optional[dict]:
        """Читает счётчики /proc/diskstats для диска."""
        try:
            text = Path("/proc/diskstats").read_text(encoding="utf-8")
        except Exception:
            return None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 14 and parts[2] == self.name:
                return {
                    "reads": int(parts[3]),
                    "sectors_read": int(parts[5]),
                    "writes": int(parts[7]),
                    "sectors_written": int(parts[9]),
                    "weighted_io": int(parts[13]),
                }
        return None

    # --- сэмплирование ---

    def _sample_once(self):
        gts, width = self._read_link()
        temp = self._read_temp()
        cur = self._read_diskstats()

        read_mbs = write_mbs = iops = avgqu_sz = 0.0
        now = time.time()
        prev = self._prev_diskstats
        prev_ts = self._prev_ts
        self._prev_diskstats = cur
        self._prev_ts = now

        if cur and prev and prev_ts is not None and now > prev_ts:
            dt = now - prev_ts
            read_bytes = (cur["sectors_read"] - prev["sectors_read"]) * self.SECTOR_SIZE
            write_bytes = (cur["sectors_written"] - prev["sectors_written"]) * self.SECTOR_SIZE
            read_mbs = read_bytes / 1e6 / dt
            write_mbs = write_bytes / 1e6 / dt
            ops = (cur["reads"] - prev["reads"]) + (cur["writes"] - prev["writes"])
            iops = ops / dt
            weighted_delta = cur["weighted_io"] - prev["weighted_io"]
            avgqu_sz = weighted_delta / 1000.0 / dt

        self.samples.append({
            "gts": gts,
            "width": width,
            "temp": temp,
            "read_mbs": read_mbs,
            "write_mbs": write_mbs,
            "iops": iops,
            "avgqu_sz": avgqu_sz,
        })

    def run(self, stop_event: threading.Event):
        """Поток-сэмплер: опрашивает источники до установки stop_event."""
        self._sample_once()
        while not stop_event.is_set():
            stop_event.wait(self.interval)
            if stop_event.is_set():
                break
            self._sample_once()

    def summary(self) -> dict:
        """Сводит собранные сэмплы в итоговый отчёт."""
        gts_vals = [s["gts"] for s in self.samples if s["gts"] is not None]
        width_vals = [s["width"] for s in self.samples if s["width"] is not None]
        temps = [s["temp"] for s in self.samples if s["temp"] is not None]
        reads = [s["read_mbs"] for s in self.samples if s["read_mbs"] > 0]
        writes = [s["write_mbs"] for s in self.samples if s["write_mbs"] > 0]
        iops_vals = [s["iops"] for s in self.samples if s["iops"] > 0]
        queues = [s["avgqu_sz"] for s in self.samples if s["avgqu_sz"] > 0]

        return {
            "link_gts_min": min(gts_vals) if gts_vals else None,
            "link_width_min": min(width_vals) if width_vals else None,
            "temp_max_c": round(max(temps), 1) if temps else None,
            "read_mbs_avg": round(sum(reads) / len(reads), 1) if reads else None,
            "write_mbs_avg": round(sum(writes) / len(writes), 1) if writes else None,
            "iops_avg": round(sum(iops_vals) / len(iops_vals)) if iops_vals else None,
            "avgqu_sz_max": round(max(queues), 1) if queues else None,
            "samples": len(self.samples),
        }
