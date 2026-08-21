"""
Модуль настройки системы для максимальной производительности накопителей.

Применяет:
- CPU governor → performance (write + verify; неудача не критична — тесты
  продолжаются, код завершения повышается до 2);
- NVMe APST → отключён для целевых NVMe (best-effort, ошибка только в отчёте).

NUMA-привязка fio (--cpus_allowed) — через get_numa_cpus(), не через apply().
"""

import sys
from pathlib import Path
import re
import subprocess
from typing import Dict, List, Optional

from rich.console import Console

console = Console(color_system=None, highlight=False)

VALID_CPULIST_RE = re.compile(r"^[\d,\-\s]+$")


class SystemTuner:
    """Настройка системы для тестирования накопителей."""

    def __init__(self, target_disks: List[dict], system_disks: Optional[List[dict]] = None):
        self.target_disks = target_disks
        self.system_disks = system_disks or []
        self._target_nvme = [
            d for d in target_disks if d.get("tran", "").lower() == "nvme"
        ]
        self.applied: List[Dict] = []
        self.governor_failed = False

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def apply(self) -> None:
        """Применяет оптимизации: governor → performance, APST → off.

        Governor — не критичная ошибка: тесты продолжаются, в конце
        работы код завершения повышается до 2 (tuner.governor_failed).
        APST — best-effort (ошибка только в отчёте, на выходной код не влияет).
        """
        self.applied = []
        self.governor_failed = False
        self._apply_cpu_governor()
        self._apply_nvme_apst()

    def preview(self) -> List[Dict]:
        """Dry-run для режима -t: что БЫЛО бы применено (read-only)."""
        rows = []

        governor_path = _governor_path()
        if governor_path is None:
            rows.append({
                "param": "CPU governor",
                "before": "cpufreq недоступен",
                "after": "performance",
                "skipped_reason": "scaling_governor не найден",
            })
        else:
            try:
                current = governor_path.read_text(encoding="utf-8").strip()
            except Exception:
                current = "?"
            if current != "performance":
                rows.append({
                    "param": "CPU governor",
                    "before": current,
                    "after": "performance",
                })

        for disk in self._target_nvme:
            apst = _read_apst(disk["path"])
            if apst == "enabled":
                rows.append({
                    "param": "NVMe APST",
                    "before": "enabled",
                    "after": "disabled",
                    "target_disks": disk["name"],
                })

        return rows

    def print_summary(self) -> None:
        """Выводит в консоль, что было применено."""
        console.print("\n[bold]Оптимизация системы...[/bold]")
        for item in self.applied:
            label = item["param"]
            if item.get("target_disks"):
                label += f" ({item['target_disks']})"
            before = item.get("before")
            arrow = f"{before} → " if before else ""
            if item["success"]:
                console.print(
                    f"  [green]✓[/green] {label}: "
                    f"{arrow}{item['after']}"
                )
            else:
                console.print(
                    f"  [yellow]—[/yellow] {label}: пропущено"
                    + (f" ({item['error']})" if item.get("error") else "")
                )
        console.print()

    def report(self) -> List[Dict]:
        """Список применённых настроек для MD-отчёта."""
        return self.applied

    def get_numa_cpus(self, disk_name: str) -> Optional[str]:
        """CPU-маска NUMA-узла диска или None."""
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

    def get_nvme_temps(self) -> Dict[str, Optional[float]]:
        """Текущие температуры NVMe в °C: {имя_контроллера: temp}."""
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
    # CPU governor: write → verify → die
    # ------------------------------------------------------------------

    def _record_governor_failure(self, error: str) -> None:
        """Фиксирует неудачу governor (не критично — тесты продолжаются)."""
        self.governor_failed = True
        self.applied.append({
            "param": "CPU governor",
            "after": "performance",
            "success": False,
            "error": error,
        })

    def _apply_cpu_governor(self) -> None:
        governor_paths = _all_governor_paths()
        if not governor_paths:
            console.print(
                "[bold red]ОШИБКА:[/bold red] cpufreq недоступен "
                "(scaling_governor не найден ни на одном CPU)"
            )
            self._record_governor_failure(
                "cpufreq недоступен (scaling_governor не найден)"
            )
            return

        # Записываем performance во все ядра
        for p in governor_paths:
            try:
                p.write_text("performance\n")
            except OSError as exc:
                console.print(
                    f"[bold red]ОШИБКА:[/bold red] не удалось записать "
                    f"governor в {p}: {exc}"
                )
                self._record_governor_failure(
                    f"не удалось записать governor в {p}: {exc}"
                )
                return

        # Верифицируем
        failed = []
        for p in governor_paths:
            try:
                current = p.read_text(encoding="utf-8").strip()
            except Exception:
                current = "?"
            if current != "performance":
                failed.append(p.parent.parent.name)

        if failed:
            console.print(
                f"[bold red]ОШИБКА:[/bold red] governor не применился "
                f"на CPU: {', '.join(failed)}"
            )
            self._record_governor_failure(
                f"governor не применился на CPU: {', '.join(failed)}"
            )
            return

        self.applied.append({
            "param": "CPU governor",
            "after": "performance",
            "success": True,
        })

    # ------------------------------------------------------------------
    # NVMe APST: disable (best-effort)
    # ------------------------------------------------------------------

    def _apply_nvme_apst(self) -> None:
        for disk in self._target_nvme:
            # Всегда пытаемся выключить APST, затем сверяем фактическое
            # состояние (аналогично governor: write → verify → record).
            # Ошибка APST не критична: тесты продолжаются, в отчёте — причина.
            try:
                result = subprocess.run(
                    ["nvme", "set-feature", disk["path"], "-f", "0x0c", "-v", "0"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                set_ok = result.returncode == 0
                set_stderr = (result.stderr or "").strip()
            except FileNotFoundError:
                set_ok = False
                set_stderr = "nvme-cli не найден в PATH"
            except (subprocess.SubprocessError, Exception):
                set_ok = False
                set_stderr = "ошибка запуска nvme-cli"

            after = _read_apst(disk["path"]) if set_ok else None
            actually_disabled = after == "disabled"
            success = set_ok and actually_disabled

            if success:
                error = ""
            elif not set_ok:
                error = set_stderr or "nvme-cli недоступен или ошибка set-feature"
            else:
                error = "APST не отключился (проверка по чтению)"

            self.applied.append({
                "param": "NVMe APST",
                "after": after if after else "недоступно",
                "success": success,
                "target_disks": disk["name"],
                "error": error,
            })


# ------------------------------------------------------------------
# Утилиты (stateless)
# ------------------------------------------------------------------

def _governor_path() -> Optional[Path]:
    """Путь к scaling_governor для cpu0 или None."""
    p = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    return p if p.exists() else None


def _all_governor_paths() -> List[Path]:
    """Все scaling_governor файлы."""
    return sorted(
        Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")
    )


def _read_apst(disk_path: str) -> Optional[str]:
    """Читает состояние APST для NVMe-диска. Возвращает 'enabled'/'disabled'/None.

    `nvme get-feature -f 0x0c` выводит состояние в виде
    ``... current value: 0xN`` (бит 0 — признак APST), либо, при -H,
    текстом ``APST: Enabled/Disabled``. Обрабатываем оба варианта.
    """
    try:
        result = subprocess.run(
            ["nvme", "get-feature", disk_path, "-f", "0x0c"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        out = result.stdout or ""
        if "APST" in out:
            if "Disabled" in out:
                return "disabled"
            if "Enabled" in out:
                return "enabled"
        m = re.search(r"current value:\s*0x([0-9a-fA-F]+)", out)
        if m:
            return "enabled" if int(m.group(1), 16) & 0x1 else "disabled"
        return "disabled"
    except (FileNotFoundError, subprocess.SubprocessError, Exception):
        return None
