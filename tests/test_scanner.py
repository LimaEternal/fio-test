import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.scanner import (
    _detect_interface,
    _link_generation,
    estimate_ceiling_mbps,
    get_nvme_pcie_info,
    link_bandwidth_mbps,
    scan_disks,
)


KNOWN_INTERFACES = {"nvme": [], "sas": [], "sata": []}


class LinkGenerationTests(unittest.TestCase):
    def test_generation_mapping(self):
        self.assertEqual(_link_generation(64.0), 6)
        self.assertEqual(_link_generation(32.0), 5)
        self.assertEqual(_link_generation(16.0), 4)
        self.assertEqual(_link_generation(8.0), 3)
        self.assertEqual(_link_generation(5.0), 2)
        self.assertEqual(_link_generation(2.5), 1)


class GetNvmePcieInfoTests(unittest.TestCase):
    """Тесты поиска линк-файлов в sysfs (включая сценарий Intel VMD)."""

    def _patch_sys_root(self, sys_dir):
        """Перенаправляет обращения к /sys/... на временный каталог."""
        root = str(sys_dir)

        def _mapped(path):
            p = str(path)
            if p.startswith("/sys"):
                return Path(root) / p[len("/sys"):].lstrip("/")
            return Path(p)

        return mock.patch("utils.scanner.Path", side_effect=_mapped)

    def test_found_via_class_nvme_when_block_device_path_is_dead_end(self):
        """VMD: /sys/block/<name>/device ведёт в тупик без линк-файлов,
        но /sys/class/nvme/<nvmeN>/device указывает на реальную PCI-функцию."""
        tmp = Path(tempfile.mkdtemp())
        # Реальная PCI-функция: линк-файлы рядом с каталогом nvme/nvme1
        pci_dir = tmp / "sys" / "class" / "nvme" / "nvme1" / "device"
        (pci_dir / "nvme" / "nvme1").mkdir(parents=True)
        (pci_dir / "current_link_speed").write_text("32.0 GT/s PCIe", encoding="utf-8")
        (pci_dir / "current_link_width").write_text("4", encoding="utf-8")
        # Тупик под /sys/block: линк-файлов нет нигде выше
        (tmp / "sys" / "block" / "nvme1n1" / "device").mkdir(parents=True)

        with self._patch_sys_root(tmp / "sys"):
            info = get_nvme_pcie_info("nvme1n1")

        self.assertEqual(info, {"gen": 5, "width": 4, "speed_gts": 32.0})

    def test_walk_up_finds_link_files_in_parent(self):
        """Линк-файлы лежат уровнем выше каталога, в который ведёт device."""
        tmp = Path(tempfile.mkdtemp())
        pci_dir = tmp / "sys" / "class" / "nvme" / "nvme2" / "device" / "pci-fn"
        pci_dir.mkdir(parents=True)
        (pci_dir.parent / "current_link_speed").write_text("32.0 GT/s", encoding="utf-8")
        (pci_dir.parent / "current_link_width").write_text("4", encoding="utf-8")

        with self._patch_sys_root(tmp / "sys"):
            info = get_nvme_pcie_info("nvme2n1")

        self.assertEqual(info, {"gen": 5, "width": 4, "speed_gts": 32.0})

    def test_no_link_files_returns_empty_info(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "sys" / "class" / "nvme" / "nvme3" / "device").mkdir(parents=True)
        (tmp / "sys" / "block" / "nvme3n1" / "device").mkdir(parents=True)

        with self._patch_sys_root(tmp / "sys"):
            info = get_nvme_pcie_info("nvme3n1")

        self.assertEqual(info, {"gen": None, "width": None, "speed_gts": None})


if __name__ == "__main__":
    unittest.main()


class DetectInterfaceTests(unittest.TestCase):
    def test_nvme_detected_by_name_even_without_transport(self):
        self.assertEqual(_detect_interface("nvme0n1", None), "nvme")

    def test_nvme_detected_by_name_with_non_standard_transport(self):
        self.assertEqual(_detect_interface("nvme1n1", "pcie"), "nvme")

    def test_nvme_detected_by_name_with_standard_transport(self):
        self.assertEqual(_detect_interface("nvme2n1", "nvme"), "nvme")

    def test_nvme_name_takes_priority_over_sata_transport(self):
        self.assertEqual(_detect_interface("nvme3n1", "sata"), "nvme")

    def test_sas_detected_from_transport(self):
        self.assertEqual(_detect_interface("sda", "sas"), "sas")
        self.assertEqual(_detect_interface("sdb", "SAS"), "sas")

    def test_sata_detected_from_transport(self):
        self.assertEqual(_detect_interface("sda", "sata"), "sata")
        self.assertEqual(_detect_interface("sdb", "SATA"), "sata")

    def test_unknown_transport_falls_back_to_sata(self):
        self.assertEqual(_detect_interface("sda", None), "sata")
        self.assertEqual(_detect_interface("sda", ""), "sata")
        self.assertEqual(_detect_interface("sda", "usb"), "sata")


class ScanDisksTests(unittest.TestCase):
    def _make_lsblk(self, blockdevices):
        proc = mock.Mock()
        proc.stdout = json.dumps({"blockdevices": blockdevices})
        proc.stderr = ""
        proc.returncode = 0
        return proc

    def test_system_disk_excluded_and_interfaces_classified(self):
        lsblk_output = [
            {
                "name": "nvme0n1", "type": "disk", "size": "3.2T",
                "model": "KIOXIA KCMY1VUG3T20", "serial": "SN-NVME-0",
                "tran": "pcie", "mountpoint": None, "phy-sec": 512, "hctl": None,
            },
            {
                "name": "nvme1n1", "type": "disk", "size": "3.2T",
                "model": "KIOXIA KCMY1VUG3T20", "serial": "SN-NVME-1",
                "tran": "nvme", "mountpoint": None, "phy-sec": 512, "hctl": None,
                "children": [
                    {
                        "name": "nvme1n1p2", "type": "part",
                        "size": "1T", "mountpoint": "/",
                    }
                ],
            },
            {
                "name": "sda", "type": "disk", "size": "1.8T",
                "model": "SEAGATE ST1800MM0129", "serial": "SN-SAS",
                "tran": "sas", "mountpoint": None, "phy-sec": 512, "hctl": "0:2:0:0",
            },
            {
                "name": "sdb", "type": "disk", "size": "960G",
                "model": "SAMSUNG PM883", "serial": "SN-SATA",
                "tran": "sata", "mountpoint": None, "phy-sec": 512, "hctl": "1:0:0:0",
            },
            {
                "name": "sdc", "type": "disk", "size": "0B",
                "model": "GHOST", "serial": "SN-GHOST",
                "tran": "sata", "mountpoint": None, "phy-sec": 512, "hctl": None,
            },
        ]

        with mock.patch(
            "utils.scanner.subprocess.run",
            return_value=self._make_lsblk(lsblk_output),
        ), mock.patch(
            "utils.scanner.get_nvme_pcie_info",
            return_value={"gen": 5, "width": 4, "speed_gts": 32.0},
        ):
            system_disks, target_disks = scan_disks(KNOWN_INTERFACES)

        self.assertEqual(len(system_disks), 1)
        self.assertEqual(system_disks[0]["name"], "nvme1n1")
        self.assertEqual(system_disks[0]["root_partition"], "nvme1n1p2")

        self.assertEqual(
            [d["name"] for d in target_disks], ["nvme0n1", "sda", "sdb"]
        )
        self.assertEqual(
            [d["tran"] for d in target_disks], ["nvme", "sas", "sata"]
        )
        self.assertEqual(target_disks[0]["pcie_info"]["gen"], 5)

    def test_missing_transport_field_does_not_break_detection(self):
        lsblk_output = [
            {
                "name": "nvme0n1", "type": "disk", "size": "1.7T",
                "model": "SAMSUNG MZWLO1T9HCJR", "serial": "SN",
                "mountpoint": None, "phy-sec": 512, "hctl": None,
            }
        ]

        with mock.patch(
            "utils.scanner.subprocess.run",
            return_value=self._make_lsblk(lsblk_output),
        ), mock.patch(
            "utils.scanner.get_nvme_pcie_info",
            return_value={"gen": None, "width": None, "speed_gts": None},
        ):
            _, target_disks = scan_disks(KNOWN_INTERFACES)

        self.assertEqual([d["name"] for d in target_disks], ["nvme0n1"])
        self.assertEqual(target_disks[0]["tran"], "nvme")


class LinkBandwidthTests(unittest.TestCase):
    """Теоретическая пропускная способность шины (без поправок на диск)."""

    def test_nvme_gen4_x4(self):
        link = {"speed_gts": 16.0, "width": 4, "gen": 4}
        self.assertAlmostEqual(link_bandwidth_mbps("nvme", link), 7876.8, places=1)

    def test_nvme_gen5_x4(self):
        link = {"speed_gts": 32.0, "width": 4, "gen": 5}
        self.assertAlmostEqual(link_bandwidth_mbps("nvme", link), 15753.6, places=1)

    def test_sas_12g(self):
        link = {"negotiated_gbps": 12.0, "maximum_gbps": 12.0}
        self.assertAlmostEqual(link_bandwidth_mbps("sas", link), 1200.0, places=1)

    def test_sata_6g(self):
        link = {"spd_limit_gbps": 6.0, "hw_spd_limit_gbps": 6.0}
        self.assertAlmostEqual(link_bandwidth_mbps("sata", link), 600.0, places=1)

    def test_no_link_returns_none(self):
        self.assertIsNone(link_bandwidth_mbps("nvme", None))

    def test_estimate_ceiling_uses_bandwidth_for_nvme(self):
        link = {"speed_gts": 32.0, "width": 4, "gen": 5}
        self.assertAlmostEqual(
            estimate_ceiling_mbps("nvme", link, 0), 15753.6, places=1
        )


if __name__ == "__main__":
    unittest.main()
