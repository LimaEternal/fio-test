"""
Модуль диагностики производительности дисков.

Во время fio-теста в отдельном потоке сэмплирует состояние системы:
    * состояние PCIe-линка (current_link_speed/current_link_width) — просадка
      поколения или ширины под нагрузкой;
    * температуру контроллера NVMe через `nvme smart-log` (nvme-cli) —
      перегрев/троттлинг;
    * реальную нагрузку на диск: посекундные логи fio
      (write_bw_log/write_iops_log) — IOPS/МБ/с. Скорость из логов вливается
      в сэмплы после завершения теста (merge_fio_logs).

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


# --- посекундные логи fio (write_bw_log / write_iops_log) ---


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
    """Парсит bw/iops-логи fio по префиксу в посекундную нагрузку.

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
    """Сэмплирует линк и температуру в отдельном потоке.

    Нагрузку (МБ/с, IOPS) сэмплер не считает: её отдаёт сам fio своими
    посекундными логами (write_bw_log/write_iops_log), которые после
    завершения теста вливаются в сэмплы через merge_fio_logs.
    """

    def __init__(self, disk: dict, interval: float = 1.0):
        self.disk = disk
        self.interval = interval
        self.name = disk.get("name", "")
        self.nvme_dev = _nvme_dev_name(self.name)
        self.link_dir = find_nvme_link_dir(self.name)
        self.samples = []
        # Какие источники реально отдали данные (для диагностики в отчёте).
        self.source_status = {"link": False, "temp": False}
        # Кэш температуры (см. TEMP_CACHE_SEC).
        self._temp_cache = None
        self._temp_cache_ts = 0.0

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

        Разные версии nvme-cli форматируют по-разному:
        `temperature: 31 C (304 Kelvin)` или `temperature: 28°C (301 Kelvin)`
        (градус может идти без пробела). Первое число перед `C` — температура.
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
            m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*C\b", line)
            if m:
                return float(m.group(1))
        return None

    def merge_fio_logs(self, prefix: str) -> bool:
        """Вливает посекундную нагрузку из логов fio в сэмплы.

        Лог-файлы fio — единственный источник нагрузки: скорость/IOPS fio
        пишет сам (write_bw_log/write_iops_log). Значения из логов матчатся
        по таймстампу (сэмплы и лог пишутся раз в секунду).

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

        if gts is not None:
            self.source_status["link"] = True
        if temp is not None:
            self.source_status["temp"] = True

        self.samples.append({
            "ts": time.time(),
            "gts": gts,
            "width": width,
            "temp": temp,
            "read_mbs": None,
            "write_mbs": None,
            "iops": None,
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

        load_source = "fio" if any(s.get("load_source") == "fio" for s in self.samples) else None

        return {
            "link_gts_min": min(gts_vals) if gts_vals else None,
            "link_width_min": min(width_vals) if width_vals else None,
            "temp_max_c": round(max(temps), 1) if temps else None,
            "read_mbs_avg": round(sum(reads) / len(reads), 1) if reads else None,
            "write_mbs_avg": round(sum(writes) / len(writes), 1) if writes else None,
            "iops_avg": round(sum(iops_vals) / len(iops_vals)) if iops_vals else None,
            "samples": len(self.samples),
            "sources": dict(self.source_status),
            "load_source": load_source,
        }
