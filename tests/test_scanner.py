import json
from pathlib import Path
import sys
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.scanner import _detect_interface, scan_disks


KNOWN_INTERFACES = {"nvme": [], "sas": [], "sata": []}


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


if __name__ == "__main__":
    unittest.main()
