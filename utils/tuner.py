"""
Модуль безопасной настройки системы для максимальной производительности NVMe.

Применяет ТОЛЬКО безопасные, не требующие перезагрузки настройки и ТОЛЬКО
к целевым (несистемным) дискам:
- CPU governor → performance (с контролем температуры);
- readahead → 2048 KB для целевых NVMe;
- NVMe APST → отключён для целевых NVMe;
- NUMA-привязка fio (--cpus_allowed) для целевых дисков.

Ничего, что влияет на системные диски или требует перезагрузки
(глобальный NVMe power-saving, CPU turbo, PCIe ASPM, kernel cmdline,
Intel VMD), не применяется и даже не выводится в предупреждения.
"""

from pathlib import Path
import re
import subprocess
from typing import Dict, List, Optional

from rich.console import Console

console = Console(color_system=None, highlight=False)

# Без этого порога governor не применяется (защита от перегрева).
MAX_TEMP_BEFORE_TUNE_C = 75
READAHEAD_KB = 2048
VALID_CPULIST_RE = re.compile(r"^[\d,\-\s]+$")


class SystemTuner:
    """Безопасная настройка системы для тестирования накопителей."""

    def __init__(self, target_disks: List[dict], system_disks: Optional[List[dict]] = None):
        """
        Параметры:
            target_disks — целевые (несистемные) диски из scanner.py.
            system_disks — системные диски (для справки, настройки их НЕ касаются).
        """
        self.target_disks = target_disks
        self.system_disks = system_disks or []
        self._target_nvme = [
            d for d in target_disks if d.get("tran", "").lower() == "nvme"
        ]
        self.issues: List[Dict] = []
        self.applied: List[Dict] = []

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def detect(self) -> None:
        """Read-only: собирает, какие оптимизации будут/могут быть применены."""
        self.issues = []
        self._detect_cpu_governor()
        self._detect_readahead()
        self._detect_nvme_apst()

    def preview(self) -> List[Dict]:
        """
        Возвращает список того, что БЫЛО БЫ применено (без применения).
        Каждый элемент: {"param", "before", "after", "target_disks"}.
        """
        self.detect()
        rows = []
        for issue in self.issues:
            rows.append({
                "param": issue["param"],
                "before": issue["current"],
                "after": issue["target"],
                "target_disks": issue.get("disks", ""),
                "skipped_reason": issue.get("skipped_reason"),
            })
        return rows

    def apply(self) -> None:
        """Применяет безопасные оптимизации (только к целевым дискам)."""
        self.applied = []
        self.detect()

        for issue in self.issues:
            if "skipped_reason" in issue:
                self.applied.append({
                    "param": issue["param"],
                    "before": issue["current"],
                    "after": issue["target"],
                    "success": False,
                    "error": issue["skipped_reason"],
                })
                continue
            try:
                result = issue["apply_func"]()
            except Exception as exc:
                result = False
                err = str(exc)
            else:
                err = ""

            self.applied.append({
                "param": issue["param"],
                "before": issue["current"],
                "after": issue["target"],
                "success": bool(result),
                "error": err,
            })

    def print_summary(self) -> None:
        """Выводит в консоль, что было применено."""
        if not self.applied:
            console.print("\n[bold]Оптимизация системы:[/bold] ничего не требуется")
            return

        console.print("\n[bold]Оптимизация системы...[/bold]")
        for item in self.applied:
            if item["success"]:
                console.print(
                    f"  [green]✓[/green] {item['param']}: "
                    f"{item['before']} → {item['after']}"
                )
            else:
                console.print(
                    f"  [yellow]—[/yellow] {item['param']}: пропущено"
                    + (f" ({item['error']})" if item["error"] else "")
                )
        console.print()

    def report(self) -> List[Dict]:
        """Список применённых настроек для MD-отчёта."""
        return self.applied

    def get_numa_cpus(self, disk_name: str) -> Optional[str]:
        """
        CPU-маска NUMA-узла, на котором находится диск, или None.

        Возвращается только проверенная маска вида "0-11,24-35".
        """
        for disk in self.target_disks:
            if disk["name"] != disk_name:
                continue
            numa_node = disk.get("numa_node")
            if numa_node is None or numa_node < 0:
                return None
            try:
                cpulist_path = Path(f"/sys/devices/system/node/node{numa_node}/cpulist")
                if not cpulist_path.exists():
                    return None
                cpulist = cpulist_path.read_text(encoding="utf-8").strip()
            except Exception:
                return None
            if cpulist and VALID_CPULIST_RE.match(cpulist):
                return cpulist
            return None
        return None

    # ------------------------------------------------------------------
    # Температура NVMe
    # ------------------------------------------------------------------

    def get_nvme_temps(self) -> Dict[str, Optional[float]]:
        """
        Текущие температуры NVMe в °C: {имя_контроллера: temp}.

        Имя — nvme0, nvme1 и т.д. (sysfs-имена hwmon).
        """
        temps: Dict[str, Optional[float]] = {}
        try:
            for ctrl in Path("/sys/class/nvme").glob("nvme*"):
                if not ctrl.is_dir():
                    continue
                for hwmon in (ctrl / "hwmon").glob("hwmon*") if (ctrl / "hwmon").is_dir() else []:
                    temp_path = hwmon / "temp1_input"
                    if temp_path.exists():
                        try:
                            raw = int(temp_path.read_text().strip())
                            temps[ctrl.name] = raw / 1000.0
                        except Exception:
                            pass
        except Exception:
            pass
        return temps

    # ------------------------------------------------------------------
    # CPU governor
    # ------------------------------------------------------------------

    def _detect_cpu_governor(self) -> None:
        governor_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if not governor_path.exists():
            return
        try:
            current = governor_path.read_text().strip()
        except Exception:
            return
        if current == "performance":
            return

        issue = {
            "param": "CPU governor",
            "current": current,
            "target": "performance",
            "disks": "все CPU",
            "apply_func": self._apply_cpu_governor,
        }
        # Проверка температуры заранее (read-only): при перегреве не применяем.
        temps = self.get_nvme_temps()
        hot = [
            (name, t)
            for name, t in temps.items()
            if t is not None and t > MAX_TEMP_BEFORE_TUNE_C
        ]
        if hot:
            names = ", ".join(f"{n} ({t:.0f}°C)" for n, t in hot)
            issue["skipped_reason"] = f"перегрев NVMe: {names}"
        self.issues.append(issue)

    def _apply_cpu_governor(self) -> bool:
        applied_any = False
        try:
            for governor_path in Path("/sys/devices/system/cpu").glob(
                "cpu*/cpufreq/scaling_governor"
            ):
                try:
                    governor_path.write_text("performance\n")
                    applied_any = True
                except Exception:
                    pass
        except Exception:
            return applied_any
        return applied_any

    # ------------------------------------------------------------------
    # Readahead (только целевые NVMe)
    # ------------------------------------------------------------------

    def _detect_readahead(self) -> None:
        low = [
            disk["name"]
            for disk in self._target_nvme
            if self._readahead_kb(disk["name"]) not in (None, READAHEAD_KB)
        ]
        if not low:
            return
        current = self._readahead_kb(low[0])
        self.issues.append({
            "param": "Readahead (NVMe)",
            "current": f"{current} KB" if current is not None else "?",
            "target": f"{READAHEAD_KB} KB",
            "disks": ", ".join(low),
            "apply_func": self._apply_readahead,
        })

    def _readahead_kb(self, disk_name: str) -> Optional[int]:
        try:
            ra_path = Path(f"/sys/block/{disk_name}/queue/read_ahead_kb")
            if ra_path.exists():
                return int(ra_path.read_text().strip())
        except Exception:
            pass
        return None

    def _apply_readahead(self) -> bool:
        ok = False
        for disk in self._target_nvme:
            try:
                ra_path = Path(f"/sys/block/{disk['name']}/queue/read_ahead_kb")
                ra_path.write_text(f"{READAHEAD_KB}\n")
                ok = True
            except Exception:
                pass
        return ok

    # ------------------------------------------------------------------
    # NVMe APST (только целевые NVMe, per-device)
    # ------------------------------------------------------------------

    def _detect_nvme_apst(self) -> None:
        if not self._target_nvme:
            return
        try:
            result = subprocess.run(
                ["nvme", "get-feature", self._target_nvme[0]["path"], "-f", "0x0c"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return
        if result.returncode != 0:
            return
        if "APST" in result.stdout and "Enabled" in result.stdout:
            disks = ", ".join(d["name"] for d in self._target_nvme)
            self.issues.append({
                "param": "NVMe APST",
                "current": "enabled",
                "target": "disabled",
                "disks": disks,
                "apply_func": self._apply_nvme_apst,
            })

    def _apply_nvme_apst(self) -> bool:
        ok = False
        for disk in self._target_nvme:
            try:
                result = subprocess.run(
                    ["nvme", "set-feature", disk["path"], "-f", "0x0c", "-v", "0"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    ok = True
            except Exception:
                pass
        return ok
