import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.hw_profile import (
    _detect_interface,
    _link_generation,
    _read_mpss,
    _read_mpss_from_config,
    _read_upstream_max_payload,
    compute_pass_thresholds,
    estimate_ceiling_mbps,
    find_nvme_link_dir,
    link_bandwidth_mbps,
    read_nvme_max_payload,
)
from utils.disk_filter import _is_occupied_device, scan_disks


KNOWN_INTERFACES = {"nvme": [], "sas": [], "sata": []}


class LinkGenerationTests(unittest.TestCase):
    def test_generation_mapping(self):
        self.assertEqual(_link_generation(64.0), 6)
        self.assertEqual(_link_generation(32.0), 5)
        self.assertEqual(_link_generation(16.0), 4)
        self.assertEqual(_link_generation(8.0), 3)
        self.assertEqual(_link_generation(5.0), 2)
        self.assertEqual(_link_generation(2.5), 1)


class FindNvmeLinkDirTests(unittest.TestCase):
    """Тесты поиска линк-файлов в sysfs (включая сценарий Intel VMD)."""

    def _patch_sys_root(self, sys_dir):
        """Перенаправляет обращения к /sys/... на временный каталог."""
        root = str(sys_dir)

        def _mapped(path):
            p = str(path)
            if p.startswith("/sys"):
                return Path(root) / p[len("/sys"):].lstrip("/")
            return Path(p)

        return mock.patch("utils.hw_profile.Path", side_effect=_mapped)

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
            link_dir = find_nvme_link_dir("nvme1n1")

        self.assertIsNotNone(link_dir)
        self.assertTrue((link_dir / "current_link_speed").exists())

    def test_walk_up_finds_link_files_in_parent(self):
        """Линк-файлы лежат уровнем выше каталога, в который ведёт device."""
        tmp = Path(tempfile.mkdtemp())
        pci_dir = tmp / "sys" / "class" / "nvme" / "nvme2" / "device" / "pci-fn"
        pci_dir.mkdir(parents=True)
        (pci_dir.parent / "current_link_speed").write_text("32.0 GT/s", encoding="utf-8")
        (pci_dir.parent / "current_link_width").write_text("4", encoding="utf-8")

        with self._patch_sys_root(tmp / "sys"):
            link_dir = find_nvme_link_dir("nvme2n1")

        self.assertIsNotNone(link_dir)
        self.assertTrue((link_dir / "current_link_speed").exists())

    def test_no_link_files_returns_none(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "sys" / "class" / "nvme" / "nvme3" / "device").mkdir(parents=True)
        (tmp / "sys" / "block" / "nvme3n1" / "device").mkdir(parents=True)

        with self._patch_sys_root(tmp / "sys"):
            link_dir = find_nvme_link_dir("nvme3n1")

        self.assertIsNone(link_dir)


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

    def _fake_profile(self, disk_name, tran):
        link = None
        if tran == "nvme":
            link = {"gen": 5, "width": 4, "speed_gts": 32.0,
                    "max_gen": 5, "max_width": 4, "max_speed_gts": 32.0,
                    "source": "sysfs", "max_payload": None}
        elif tran == "sas":
            link = {"negotiated_gbps": 12.0, "maximum_gbps": 12.0, "source": "sas_phy"}
        elif tran == "sata":
            link = {"spd_limit_gbps": 6.0, "hw_spd_limit_gbps": 6.0, "source": "ata_link"}
        return {"interface": tran, "rotational": 0, "link": link, "ceiling_mbps": 0}

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
            "utils.disk_filter.subprocess.run",
            return_value=self._make_lsblk(lsblk_output),
        ), mock.patch(
            "utils.disk_filter.collect_hw_profile",
            side_effect=self._fake_profile,
        ):
            system_disks, occupied_disks, target_disks = scan_disks(KNOWN_INTERFACES)

        self.assertEqual(len(system_disks), 1)
        self.assertEqual(system_disks[0]["name"], "nvme1n1")
        self.assertEqual(system_disks[0]["root_partition"], "nvme1n1p2")

        self.assertEqual(occupied_disks, [])

        self.assertEqual(
            [d["name"] for d in target_disks], ["nvme0n1", "sda", "sdb"]
        )
        self.assertEqual(
            [d["tran"] for d in target_disks], ["nvme", "sas", "sata"]
        )
        self.assertEqual(target_disks[0]["profile"]["link"]["gen"], 5)

    def test_missing_transport_field_does_not_break_detection(self):
        lsblk_output = [
            {
                "name": "nvme0n1", "type": "disk", "size": "1.7T",
                "model": "SAMSUNG MZWLO1T9HCJR", "serial": "SN",
                "mountpoint": None, "phy-sec": 512, "hctl": None,
            }
        ]

        with mock.patch(
            "utils.disk_filter.subprocess.run",
            return_value=self._make_lsblk(lsblk_output),
        ):
            _, _, target_disks = scan_disks(KNOWN_INTERFACES)

        self.assertEqual([d["name"] for d in target_disks], ["nvme0n1"])
        self.assertEqual(target_disks[0]["tran"], "nvme")


class OccupiedDetectionTests(unittest.TestCase):
    """Диски с данными (разделы/ФС/монтирование) исключаются из тестов."""

    def _make_lsblk(self, blockdevices):
        proc = mock.Mock()
        proc.stdout = json.dumps({"blockdevices": blockdevices})
        proc.stderr = ""
        proc.returncode = 0
        return proc

    def test_partition_table_is_occupied(self):
        dev = {"name": "nvme0n1", "type": "disk", "children": [
            {"name": "nvme0n1p1", "type": "part", "fstype": None},
        ]}
        self.assertTrue(_is_occupied_device(dev))

    def test_filesystem_is_occupied(self):
        dev = {"name": "sda", "type": "disk", "children": [
            {"name": "sda1", "type": "part", "fstype": "ext4"},
        ]}
        self.assertTrue(_is_occupied_device(dev))

    def test_mounted_anywhere_is_occupied(self):
        dev = {"name": "sdb", "type": "disk", "children": [
            {"name": "sdb1", "type": "part", "mountpoint": "/mnt/data"},
        ]}
        self.assertTrue(_is_occupied_device(dev))

    def test_blank_disk_is_not_occupied(self):
        dev = {"name": "nvme0n1", "type": "disk", "mountpoint": None, "fstype": None}
        self.assertFalse(_is_occupied_device(dev))

    def test_scan_disks_three_way_split(self):
        lsblk_output = [
            {
                "name": "nvme0n1", "type": "disk", "size": "1.7T",
                "model": "SAMSUNG", "serial": "SN0", "tran": "nvme",
                "mountpoint": None, "fstype": None, "phy-sec": 512, "hctl": None,
                "children": [
                    {"name": "nvme0n1p1", "type": "part", "fstype": "ext4",
                     "mountpoint": None},
                ],
            },
            {
                "name": "nvme1n1", "type": "disk", "size": "1.7T",
                "model": "SAMSUNG", "serial": "SN1", "tran": "nvme",
                "mountpoint": None, "fstype": None, "phy-sec": 512, "hctl": None,
            },
            {
                "name": "sda", "type": "disk", "size": "1.8T",
                "model": "SEAGATE", "serial": "SNS", "tran": "sas",
                "mountpoint": None, "fstype": None, "phy-sec": 512, "hctl": "0:2:0:0",
                "children": [
                    {"name": "sda1", "type": "part", "mountpoint": "/"},
                ],
            },
        ]

        with mock.patch(
            "utils.disk_filter.subprocess.run",
            return_value=self._make_lsblk(lsblk_output),
        ), mock.patch(
            "utils.disk_filter.collect_hw_profile",
            return_value={"interface": "nvme", "rotational": 0, "link": None},
        ):
            system_disks, occupied_disks, target_disks = scan_disks(KNOWN_INTERFACES)

        self.assertEqual([d["name"] for d in system_disks], ["sda"])
        self.assertEqual([d["name"] for d in occupied_disks], ["nvme0n1"])
        self.assertEqual([d["name"] for d in target_disks], ["nvme1n1"])
        self.assertTrue(occupied_disks[0]["occupied"])


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


class NvmeMaxPayloadTests(unittest.TestCase):
    """Чтение MaxPayload (размер TLP PCIe) NVMe-контроллера из sysfs."""

    def test_read_mpss_ok(self):
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "256"
        with mock.patch("utils.hw_profile.Path", return_value=fake_path):
            self.assertEqual(_read_mpss("0000:01:00.0"), 256)

    def test_read_mpss_missing(self):
        fake_path = mock.Mock()
        fake_path.exists.return_value = False
        with mock.patch("utils.hw_profile.Path", return_value=fake_path):
            self.assertIsNone(_read_mpss("0000:01:00.0"))

    def test_read_mpss_bad_value(self):
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "garbage"
        fake_path.read_bytes.return_value = b""  # config fallback -> слишком короткий -> None
        with mock.patch("utils.hw_profile.Path", return_value=fake_path):
            self.assertIsNone(_read_mpss("0000:01:00.0"))

    @staticmethod
    def _make_config(encoding: int, cap_ptr: int = 0x40) -> bytes:
        """Синтетический PCI config space с PCIe Capability (DevCap)."""
        blob = bytearray(256)
        blob[0x34] = cap_ptr          # Capabilities Pointer
        blob[cap_ptr] = 0x10          # PCI Express Capability ID
        blob[cap_ptr + 1] = 0x00      # Next Capability (none)
        devcap = encoding             # bits 2:0 = MaxPayload encoding
        blob[cap_ptr + 4:cap_ptr + 8] = devcap.to_bytes(4, "little")
        return bytes(blob)

    def test_read_mpss_from_config_encodings(self):
        for encoding, expected in [(0, 128), (1, 256), (2, 512), (3, 1024)]:
            cfg = self._make_config(encoding)
            fake_path = mock.Mock()
            fake_path.exists.return_value = True
            fake_path.read_bytes.return_value = cfg
            with mock.patch("utils.hw_profile.Path", return_value=fake_path):
                self.assertEqual(_read_mpss_from_config("0000:01:00.0"), expected)

    def test_read_mpss_from_config_no_pcie_cap(self):
        blob = bytearray(256)
        blob[0x34] = 0x40
        blob[0x40] = 0x05  # Vendor-Specific capability, not PCIe
        blob[0x41] = 0x00
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        fake_path.read_bytes.return_value = bytes(blob)
        with mock.patch("utils.hw_profile.Path", return_value=fake_path):
            self.assertIsNone(_read_mpss_from_config("0000:01:00.0"))

    def test_read_mpss_from_config_missing(self):
        fake_path = mock.Mock()
        fake_path.exists.return_value = False
        with mock.patch("utils.hw_profile.Path", return_value=fake_path):
            self.assertIsNone(_read_mpss_from_config("0000:01:00.0"))

    def test_read_mpss_config_fallback(self):
        # mpss отсутствует, config есть -> декодируем из config space
        mpss_path = mock.Mock()
        mpss_path.exists.return_value = False
        cfg_path = mock.Mock()
        cfg_path.exists.return_value = True
        cfg_path.read_bytes.return_value = self._make_config(2)  # 512 B

        def _path_side_effect(p):
            return cfg_path if str(p).endswith("/config") else mpss_path

        with mock.patch("utils.hw_profile.Path", side_effect=_path_side_effect):
            self.assertEqual(_read_mpss("0000:01:00.0"), 512)

    def test_read_device_and_port(self):
        link_dir = Path("/sys/bus/pci/devices/0000:01:00.0")
        with mock.patch(
            "utils.hw_profile.find_nvme_link_dir", return_value=link_dir
        ), mock.patch(
            "utils.hw_profile._read_mpss", return_value=256
        ), mock.patch(
            "utils.hw_profile._read_upstream_max_payload", return_value=512
        ):
            result = read_nvme_max_payload("nvme0n1")
        self.assertEqual(result, {"device": 256, "port": 512})

    def test_read_device_only_when_port_unavailable(self):
        link_dir = Path("/sys/bus/pci/devices/0000:01:00.0")
        with mock.patch(
            "utils.hw_profile.find_nvme_link_dir", return_value=link_dir
        ), mock.patch(
            "utils.hw_profile._read_mpss", return_value=128
        ), mock.patch(
            "utils.hw_profile._read_upstream_max_payload", return_value=None
        ):
            result = read_nvme_max_payload("nvme0n1")
        self.assertEqual(result, {"device": 128, "port": None})

    def test_no_link_dir_returns_none(self):
        with mock.patch(
            "utils.hw_profile.find_nvme_link_dir", return_value=None
        ):
            self.assertIsNone(read_nvme_max_payload("nvme0n1"))

    def test_read_upstream_mpss(self):
        fake_dev = mock.Mock()
        fake_dev.exists.return_value = True
        fake_bridge = mock.Mock()
        fake_bridge.name = "0000:00:01.0"
        fake_dev.resolve.return_value.parent = fake_bridge
        with mock.patch(
            "utils.hw_profile.Path", return_value=fake_dev
        ), mock.patch(
            "utils.hw_profile._read_mpss", return_value=512
        ):
            result = _read_upstream_max_payload("0000:01:00.0")
        self.assertEqual(result, 512)

    def test_read_upstream_no_parent(self):
        fake_dev = mock.Mock()
        fake_dev.exists.return_value = True
        fake_dev.name = "0000:01:00.0"
        fake_dev.resolve.return_value.parent = fake_dev
        with mock.patch("utils.hw_profile.Path", return_value=fake_dev):
            result = _read_upstream_max_payload("0000:01:00.0")
        self.assertIsNone(result)


class ComputePassThresholdsTests(unittest.TestCase):
    """Динамические пороги PASS/FAIL из профиля (ТЗ Zero-Config)."""

    def _disk(self, interface, rotational=0, link=None):
        return {"tran": interface, "profile": {
            "interface": interface, "rotational": rotational, "link": link,
        }}

    def test_nvme_gen5_x4(self):
        d = self._disk("nvme", link={"width": 4, "speed_gts": 32.0})
        thr = compute_pass_thresholds(d)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 12477.0, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 6612.8, places=1)

    def test_nvme_gen4_x4(self):
        d = self._disk("nvme", link={"width": 4, "speed_gts": 16.0})
        thr = compute_pass_thresholds(d)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 6238.5, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 3306.4, places=0)

    def test_nvme_gen3_x4(self):
        d = self._disk("nvme", link={"width": 4, "speed_gts": 8.0})
        thr = compute_pass_thresholds(d)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 3083.8, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 1634.4, places=0)

    def test_sata_iii(self):
        d = self._disk("sata", link={"spd_limit_gbps": 6.0})
        thr = compute_pass_thresholds(d)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 495.0, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 445.5, places=0)

    def test_sas_12g(self):
        d = self._disk("sas", link={"negotiated_gbps": 12.0})
        thr = compute_pass_thresholds(d)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 1035.0, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 931.5, places=0)

    def test_hdd_media_only(self):
        d = self._disk("sata", rotational=1, link={"spd_limit_gbps": 3.0})
        thr = compute_pass_thresholds(d)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 198.0, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 198.0, places=0)

    def test_missing_sysfs_returns_empty(self):
        # NVMe без линка → данных нет, пороги считаются из конфига (fallback)
        self.assertEqual(compute_pass_thresholds(self._disk("nvme")), {})
        self.assertEqual(compute_pass_thresholds(self._disk("sata")), {})

    def test_target_percent_scales(self):
        d = self._disk("sata", link={"spd_limit_gbps": 6.0})
        thr = compute_pass_thresholds(d, target_percent=0.50)
        self.assertAlmostEqual(thr["seq_read"]["min_bw_mb"], 275.0, places=0)
        self.assertAlmostEqual(thr["seq_write"]["min_bw_mb"], 247.5, places=0)


if __name__ == "__main__":
    unittest.main()
