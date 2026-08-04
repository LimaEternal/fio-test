"""
Модуль автоматической настройки системы для максимальной производительности NVMe.

Проверяет и применяет параметры, влияющие на скорость SSD:
- CPU governor → performance
- CPU turbo → включён
- NVMe power-saving → отключён
- NVMe APST → отключён
- Readahead → 2048 KB для NVMe
- NUMA-привязка для fio
- Предупреждения о PCIe ASPM, kernel cmdline, Intel VMD
"""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Console

console = Console(color_system=None, highlight=False)


class SystemTuner:
    """Автоматическая настройка системы для тестирования NVMe."""

    def __init__(self, disks: List[dict]):
        """
        Инициализация тюнера.

        Параметры:
            disks — список словарей с информацией о дисках (из scanner.py)
        """
        self.disks = disks
        self.issues: List[Dict] = []
        self.applied: List[Dict] = []
        self.warnings: List[Dict] = []
        self._nvme_devices = [d for d in disks if d.get("tran", "").lower() == "nvme"]

    def detect(self):
        """Собирает информацию о текущих настройках и выявляет проблемы."""
        self.issues = []
        self.warnings = []

        self._detect_cpu_governor()
        self._detect_cpu_turbo()
        self._detect_nvme_power_save()
        self._detect_nvme_apst()
        self._detect_readahead()
        self._detect_pcie_aspm()
        self._detect_kernel_cmdline()
        self._detect_vmd()

    def apply(self):
        """Применяет все исправления (кроме тех, что требуют reboot)."""
        self.applied = []

        for issue in self.issues:
            if issue.get("reboot_required"):
                continue

            try:
                result = issue["apply_func"]()
                if result:
                    self.applied.append({
                        "param": issue["param"],
                        "before": issue["current"],
                        "after": issue["target"],
                        "success": True,
                    })
                else:
                    self.applied.append({
                        "param": issue["param"],
                        "before": issue["current"],
                        "after": issue["target"],
                        "success": False,
                        "error": "Не удалось применить",
                    })
            except Exception as e:
                self.applied.append({
                    "param": issue["param"],
                    "before": issue["current"],
                    "after": issue["target"],
                    "success": False,
                    "error": str(e),
                })

    def report(self) -> List[Dict]:
        """Возвращает список применённых настроек для отчёта."""
        return self.applied

    def print_summary(self):
        """Выводит в консоль summary применённых настроек и предупреждений."""
        if not self.applied and not self.warnings:
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
                    f"  [red]✗[/red] {item['param']}: "
                    f"ошибка — {item.get('error', 'неизвестно')}"
                )

        for warning in self.warnings:
            console.print(
                f"  [yellow]⚠[/yellow] {warning['message']}"
            )

        console.print()

    def drop_caches(self):
        """Сбрасывает page cache, dentries и inodes перед тестом."""
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
        except Exception:
            pass

    def get_numa_cpus(self, disk_name: str) -> Optional[str]:
        """
        Возвращает CPU-маску для NUMA-узла, на котором находится диск.

        Параметры:
            disk_name — имя диска (например, nvme0n1)

        Возвращает:
            Строка с CPU-маской (например, "0-11,24-35") или None
        """
        for disk in self.disks:
            if disk["name"] == disk_name:
                numa_node = disk.get("numa_node")
                if numa_node is None or numa_node < 0:
                    return None

                try:
                    cpulist_path = Path(f"/sys/devices/system/node/node{numa_node}/cpulist")
                    if cpulist_path.exists():
                        return cpulist_path.read_text().strip()
                except Exception:
                    pass
        return None

    def _detect_cpu_governor(self):
        """Проверяет CPU governor."""
        try:
            governor_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
            if not governor_path.exists():
                return

            current = governor_path.read_text().strip()
            if current != "performance":
                self.issues.append({
                    "param": "CPU governor",
                    "current": current,
                    "target": "performance",
                    "reboot_required": False,
                    "apply_func": self._apply_cpu_governor,
                })
        except Exception:
            pass

    def _apply_cpu_governor(self) -> bool:
        """Переключает все CPU в performance."""
        try:
            cpu_dirs = Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")
            for governor_path in cpu_dirs:
                governor_path.write_text("performance\n")
            return True
        except Exception:
            return False

    def _detect_cpu_turbo(self):
        """Проверяет состояние CPU turbo."""
        try:
            no_turbo_path = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
            if not no_turbo_path.exists():
                return

            no_turbo = int(no_turbo_path.read_text().strip())
            if no_turbo == 1:
                self.issues.append({
                    "param": "CPU turbo",
                    "current": "disabled",
                    "target": "enabled",
                    "reboot_required": False,
                    "apply_func": self._apply_cpu_turbo,
                })
        except Exception:
            pass

    def _apply_cpu_turbo(self) -> bool:
        """Включает CPU turbo."""
        try:
            no_turbo_path = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
            no_turbo_path.write_text("0\n")
            return True
        except Exception:
            return False

    def _detect_nvme_power_save(self):
        """Проверяет NVMe power-saving."""
        try:
            ps_path = Path("/sys/module/nvme_core/parameters/default_ps_max_latency_us")
            if not ps_path.exists():
                return

            current = int(ps_path.read_text().strip())
            if current > 0:
                self.issues.append({
                    "param": "NVMe power-saving",
                    "current": f"{current} us",
                    "target": "0 us",
                    "reboot_required": False,
                    "apply_func": self._apply_nvme_power_save,
                })
        except Exception:
            pass

    def _apply_nvme_power_save(self) -> bool:
        """Отключает NVMe power-saving."""
        try:
            ps_path = Path("/sys/module/nvme_core/parameters/default_ps_max_latency_us")
            ps_path.write_text("0\n")
            return True
        except Exception:
            return False

    def _detect_nvme_apst(self):
        """Проверяет NVMe APST (Autonomous Power State Transition)."""
        if not self._nvme_devices:
            return

        try:
            for disk in self._nvme_devices:
                dev_path = disk["path"]
                result = subprocess.run(
                    ["nvme", "get-feature", dev_path, "-f", "0x0c"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    if "APST" in result.stdout and "Enabled" in result.stdout:
                        self.issues.append({
                            "param": "NVMe APST",
                            "current": "enabled",
                            "target": "disabled",
                            "reboot_required": False,
                            "apply_func": self._apply_nvme_apst,
                        })
                        break
        except Exception:
            pass

    def _apply_nvme_apst(self) -> bool:
        """Отключает NVMe APST."""
        try:
            for disk in self._nvme_devices:
                dev_path = disk["path"]
                subprocess.run(
                    ["nvme", "set-feature", dev_path, "-f", "0x0c", "-v", "0"],
                    capture_output=True,
                    timeout=5,
                )
            return True
        except Exception:
            return False

    def _detect_readahead(self):
        """Проверяет readahead для NVMe."""
        try:
            for disk in self._nvme_devices:
                disk_name = disk["name"]
                ra_path = Path(f"/sys/block/{disk_name}/queue/read_ahead_kb")
                if not ra_path.exists():
                    continue

                current = int(ra_path.read_text().strip())
                if current < 2048:
                    self.issues.append({
                        "param": f"Readahead ({disk_name})",
                        "current": f"{current} KB",
                        "target": "2048 KB",
                        "reboot_required": False,
                        "apply_func": lambda d=disk_name: self._apply_readahead(d),
                    })
        except Exception:
            pass

    def _apply_readahead(self, disk_name: str) -> bool:
        """Устанавливает readahead в 2048 KB."""
        try:
            ra_path = Path(f"/sys/block/{disk_name}/queue/read_ahead_kb")
            ra_path.write_text("2048\n")
            return True
        except Exception:
            return False

    def _detect_pcie_aspm(self):
        """Проверяет PCIe ASPM на NVMe устройствах."""
        try:
            for disk in self._nvme_devices:
                disk_name = disk["name"]
                device_path = Path(f"/sys/class/nvme/{disk_name}/device")
                if not device_path.exists():
                    continue

                aspm_path = device_path / "link" / "aspm"
                if aspm_path.exists():
                    current = aspm_path.read_text().strip()
                    if current != "off":
                        self.warnings.append({
                            "message": f"PCIe ASPM: включён на {disk_name} (требуется reboot с pcie_aspm=off)",
                        })
                        break
        except Exception:
            pass

    def _detect_kernel_cmdline(self):
        """Проверяет kernel cmdline на наличие нужных параметров."""
        try:
            cmdline_path = Path("/proc/cmdline")
            if not cmdline_path.exists():
                return

            cmdline = cmdline_path.read_text().strip()
            if "nvme_core.default_ps_max_latency_us=0" not in cmdline:
                self.warnings.append({
                    "message": "Kernel cmdline: отсутствует nvme_core.default_ps_max_latency_us=0 (требуется reboot)",
                })
        except Exception:
            pass

    def _detect_vmd(self):
        """Обнаруживает Intel VMD в цепочке."""
        try:
            result = subprocess.run(
                ["lspci"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "Volume Management Device" in result.stdout:
                self.warnings.append({
                    "message": "Intel VMD: обнаружен (оверхед ~5-10%, bypass через BIOS)",
                })
        except Exception:
            pass
