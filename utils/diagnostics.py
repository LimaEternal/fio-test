"""
Модуль диагностики производительности дисков.

Во время fio-теста в отдельном потоке сэмплирует состояние системы:
    * состояние PCIe-линка (current_link_speed/current_link_width) — просадка
      поколения или ширины под нагрузкой;
    * температуру контроллера NVMe через `nvme smart-log` (nvme-cli) —
      перегрев/троттлинг;
    * реальную нагрузку на диск: /proc/diskstats и, как основной источник,
      пер-секундные логи fio (write_bw_log/write_iops_log) — IOPS/МБ/с.

Также собирает статичную информацию: NUMA-нода диска и привязка CPU.
"""

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from utils.scanner import find_nvme_link_dir

# Как часто перечитывать температуру: nvme smart-log дёргает subprocess,
# поэтому результат кэшируется (не чаще раза в TEMP_CACHE_SEC секунд).
TEMP_CACHE_SEC = 5.0


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


# --- пер-секундные логи fio (write_bw_log / write_iops_log) ---


def _log_files(prefix: str, kind: str):
    """Все log-файлы `*_<kind>*.log` по префиксу.

    С per_job_logs=1 fio пишет по файлу на job: `<prefix>_bw.0.log` и т.п.
    """
    base = Path(prefix)
    if not base.parent.exists():
        return []
    return sorted(base.parent.glob(f"{base.name}*_{kind}*.log"))


def _parse_log_file(path: Path) -> dict:
    """Читает один fio-лог в {ts_sec: {ddir: max_value}}.

    Формат строки: <time_msec>, <value>, <ddir>[, ...]. Внутри одного файла
    дубликаты таймстампа (финальный flush неполного окна) схлопываются по max.
    """
    agg = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    ts_ms = int(parts[0].strip())
                    value = float(parts[1].strip())
                    ddir = int(parts[2].strip())
                except ValueError:
                    continue
                if value <= 0:
                    continue
                ts = round(ts_ms / 1000.0)
                bucket = agg.setdefault(ts, {})
                bucket[ddir] = max(bucket.get(ddir, 0.0), value)
    except OSError:
        return {}
    return agg


def parse_fio_logs(prefix: str) -> Optional[dict]:
    """Парсит bw/iops-логи fio по префиксу в пер-секундную нагрузку.

    Возвращает {ts_sec: {"read_mbs", "write_mbs", "iops"}} либо None, если
    файлы не найдены или не распарсились. Значения суммируются по всем
    job-файлам. Лог-файлы удаляются после чтения.
    """
    bw = {}
    for path in _log_files(prefix, "bw"):
        for ts, dirs in _parse_log_file(path).items():
            for ddir, val in dirs.items():
                bw.setdefault(ts, {}).setdefault(ddir, 0.0)
                bw[ts][ddir] += val
    iops = {}
    for path in _log_files(prefix, "iops"):
        for ts, dirs in _parse_log_file(path).items():
            for ddir, val in dirs.items():
                iops.setdefault(ts, {}).setdefault(ddir, 0.0)
                iops[ts][ddir] += val

    for kind in ("bw", "iops"):
        for path in _log_files(prefix, kind):
            try:
                path.unlink()
            except OSError:
                pass

    if not bw:
        return None

    result = {}
    for ts, dirs in bw.items():
        read_mbs = dirs.get(0, 0.0) * 1024 / 1e6
        write_mbs = dirs.get(1, 0.0) * 1024 / 1e6
        if read_mbs == 0.0 and write_mbs == 0.0:
            continue
        iops_total = sum((iops.get(ts) or {}).values())
        result[ts] = {
            "read_mbs": read_mbs,
            "write_mbs": write_mbs,
            "iops": iops_total,
        }

    # Страховка для старых fio 3.x, где IOPS в логе писались x1000.
    if result and max(v["iops"] for v in result.values()) > 50_000_000:
        for v in result.values():
            v["iops"] /= 1000.0

    return result or None


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
        # Какие источники реально отдали данные (для диагностики в отчёте).
        self.source_status = {"link": False, "temp": False, "diskstats": False}
        self._first_diskstats = None
        # Кэш температуры (см. TEMP_CACHE_SEC) и признак реальной активности
        # диска по /proc/diskstats (иначе нагрузку берём из логов fio).
        self._temp_cache = None
        self._temp_cache_ts = 0.0
        self.diskstats_activity = False

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
        """Возвращает температуру контроллера NVMe в °C через nvme smart-log.

        Результат кэшируется на TEMP_CACHE_SEC секунд, чтобы не дёргать
        subprocess каждый сэмпл. Сначала пробуем контроллер (/dev/nvmeX),
        при ошибке — namespace (/dev/nvmeXn1).
        """
        if not self.nvme_dev:
            return None
        now = time.time()
        if self._temp_cache is not None and now - self._temp_cache_ts < TEMP_CACHE_SEC:
            return self._temp_cache
        for dev in (f"/dev/{self.nvme_dev}", self.disk.get("path", "")):
            if not dev or not dev.startswith("/dev/"):
                continue
            value = self._nvme_smart_temp(dev)
            if value is not None:
                self._temp_cache = value
                self._temp_cache_ts = now
                return value
        self._temp_cache = None
        self._temp_cache_ts = now
        return None

    def _nvme_smart_temp(self, dev: str) -> Optional[float]:
        """Запускает `nvme smart-log <dev>` и вытаскивает temperature.

        Формат строки: `temperature: 31 C (304 Kelvin)` — первое число
        перед `C` и есть температура в °C.
        """
        try:
            proc = subprocess.run(
                ["nvme", "smart-log", dev],
                capture_output=True, timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        text = proc.stdout.decode(errors="replace")
        for line in text.splitlines():
            if not line.strip().lower().startswith("temperature"):
                continue
            m = re.search(r"(\d+(?:\.\d+)?)\s*C\b", line)
            if m:
                return float(m.group(1))
        return None

    def _read_diskstats(self) -> Optional[dict]:
        """Читает счётчики /proc/diskstats для диска.

        Устройство ищем по имени (nvme1n1) либо по major:minor из
        /sys/class/block/<имя>/dev — надёжнее на системах, где имена в
        /proc/diskstats могут отличаться.
        """
        try:
            text = Path("/proc/diskstats").read_text(encoding="utf-8")
        except Exception:
            return None
        dev_id = None
        if self.name:
            try:
                dev_id = Path(f"/sys/class/block/{self.name}/dev").read_text(
                    encoding="utf-8").strip()
            except Exception:
                dev_id = None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            if parts[2] == self.name or (
                dev_id and f"{parts[0]}:{parts[1]}" == dev_id
            ):
                return {
                    "reads": int(parts[3]),
                    "sectors_read": int(parts[5]),
                    "writes": int(parts[7]),
                    "sectors_written": int(parts[9]),
                    "weighted_io": int(parts[13]),
                }
        return None

    def merge_fio_logs(self, prefix: str) -> bool:
        """Вливает пер-секундную нагрузку из логов fio в сэмплы.

        Лог-файлы fio — основной источник нагрузки: /proc/diskstats на
        некоторых ядрах не учитывает I/O под нагрузкой. Значения из логов
        матчатся по таймстампу (сэмплы и лог пишутся раз в секунду).
        avgqu_sz из логов взять нельзя — остаётся как был (None).

        Возвращает True, если хотя бы один сэмпл получил данные из логов.
        """
        data = parse_fio_logs(prefix)
        if not data:
            return False
        matched = False
        for s in self.samples:
            ts = s.get("ts")
            row = data.get(round(ts)) if ts is not None else None
            if row is None:
                continue
            s["read_mbs"] = row["read_mbs"]
            s["write_mbs"] = row["write_mbs"]
            s["iops"] = row["iops"]
            s["load_source"] = "fio"
            if row["read_mbs"] > 0 or row["write_mbs"] > 0:
                matched = True
        return matched

    # --- сэмплирование ---

    def _sample_once(self):
        gts, width = self._read_link()
        temp = self._read_temp()
        cur = self._read_diskstats()

        if gts is not None:
            self.source_status["link"] = True
        if temp is not None:
            self.source_status["temp"] = True
        if cur is not None:
            self.source_status["diskstats"] = True
            if self._first_diskstats is None:
                self._first_diskstats = cur

        # None, а не 0.0: в отчёте это «—» (источник не отдал данные),
        # а не «0.0» (реально нулевая нагрузка).
        read_mbs = write_mbs = iops = avgqu_sz = None
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
            if read_bytes > 0 or write_bytes > 0 or ops > 0:
                self.diskstats_activity = True

        self.samples.append({
            "ts": now,
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
        reads = [s["read_mbs"] for s in self.samples
                 if s["read_mbs"] is not None and s["read_mbs"] > 0]
        writes = [s["write_mbs"] for s in self.samples
                  if s["write_mbs"] is not None and s["write_mbs"] > 0]
        iops_vals = [s["iops"] for s in self.samples
                     if s["iops"] is not None and s["iops"] > 0]
        queues = [s["avgqu_sz"] for s in self.samples
                  if s["avgqu_sz"] is not None and s["avgqu_sz"] > 0]

        load_source = None
        if any(s.get("load_source") == "fio" for s in self.samples):
            load_source = "fio"
        elif self.diskstats_activity:
            load_source = "diskstats"

        return {
            "link_gts_min": min(gts_vals) if gts_vals else None,
            "link_width_min": min(width_vals) if width_vals else None,
            "temp_max_c": round(max(temps), 1) if temps else None,
            "read_mbs_avg": round(sum(reads) / len(reads), 1) if reads else None,
            "write_mbs_avg": round(sum(writes) / len(writes), 1) if writes else None,
            "iops_avg": round(sum(iops_vals) / len(iops_vals)) if iops_vals else None,
            "avgqu_sz_max": round(max(queues), 1) if queues else None,
            "samples": len(self.samples),
            "sources": dict(self.source_status),
            "diskstats_first": self._first_diskstats,
            "load_source": load_source,
            "diskstats_activity": self.diskstats_activity,
        }
