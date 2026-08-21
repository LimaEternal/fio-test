import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils import nvme_admin
from utils.tuner import (
    SystemTuner,
    _governor_path,
    _all_governor_paths,
    _read_apst,
    _apst_supported,
    _APSTA_OFFSET,
)

TARGET_NVME = [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "tran": "NVME"},
]
TARGET_SATA = [
    {"name": "sda", "path": "/dev/sda", "tran": "SATA"},
]


def _identify_ctrl_result(apsta_byte):
    """Фейковый nvme_admin.admin_cmd: заполняет буфер Identify Controller."""
    def _fake(disk_path, opcode, cdw10=0, cdw11=0, nsid=0,
              out_buf=None, timeout_ms=5000):
        if out_buf is not None:
            out_buf[_APSTA_OFFSET] = apsta_byte
        return nvme_admin.AdminResult(True, 0, None, "")
    return _fake


class GovernorPathTests(unittest.TestCase):
    def test_governor_path_exists(self):
        with mock.patch("utils.tuner.Path") as MockPath:
            fake_path = mock.Mock()
            fake_path.exists.return_value = True
            MockPath.return_value = fake_path
            result = _governor_path()
            self.assertEqual(result, fake_path)

    def test_governor_path_missing(self):
        with mock.patch("utils.tuner.Path") as MockPath:
            fake_path = mock.Mock()
            fake_path.exists.return_value = False
            MockPath.return_value = fake_path
            result = _governor_path()
            self.assertIsNone(result)


class AllGovernorPathsTests(unittest.TestCase):
    def test_returns_sorted(self):
        with mock.patch("utils.tuner.Path") as MockPath:
            fake_root = mock.Mock()
            p1 = mock.Mock()
            p1.__lt__ = lambda self, other: str(self) < str(other)
            p1.__str__ = lambda self: "cpu1"
            p2 = mock.Mock()
            p2.__lt__ = lambda self, other: str(self) < str(other)
            p2.__str__ = lambda self: "cpu0"
            fake_root.glob.return_value = [p1, p2]
            MockPath.return_value = fake_root
            result = _all_governor_paths()
            self.assertEqual(result, [p2, p1])


class ReadApstTests(unittest.TestCase):
    def test_enabled(self):
        res = nvme_admin.AdminResult(True, 0x1, None, "")
        with mock.patch("utils.tuner.nvme_admin.admin_cmd", return_value=res) as m:
            result = _read_apst("/dev/nvme0n1")
            self.assertEqual(result, "enabled")
            args, kwargs = m.call_args
            self.assertEqual(args[0], "/dev/nvme0n1")
            self.assertEqual(args[1], nvme_admin.OPC_GET_FEATURES)
            self.assertEqual(kwargs.get("cdw10"), nvme_admin.FID_APST)

    def test_disabled(self):
        res = nvme_admin.AdminResult(True, 0x0, None, "")
        with mock.patch("utils.tuner.nvme_admin.admin_cmd", return_value=res):
            result = _read_apst("/dev/nvme0n1")
            self.assertEqual(result, "disabled")

    def test_result_high_bit_only_is_disabled(self):
        res = nvme_admin.AdminResult(True, 0xFFFFFFFE, None, "")
        with mock.patch("utils.tuner.nvme_admin.admin_cmd", return_value=res):
            self.assertEqual(_read_apst("/dev/nvme0n1"), "disabled")

    def test_command_failure_returns_none(self):
        res = nvme_admin.AdminResult(False, 0, errno.EIO,
                                     "/dev/nvme0: Input/output error")
        with mock.patch("utils.tuner.nvme_admin.admin_cmd", return_value=res):
            self.assertIsNone(_read_apst("/dev/nvme0n1"))

    def test_device_open_failure_returns_none(self):
        res = nvme_admin.AdminResult(False, 0, errno.ENOENT,
                                     "/dev/nvme0: No such file or directory")
        with mock.patch("utils.tuner.nvme_admin.admin_cmd", return_value=res):
            self.assertIsNone(_read_apst("/dev/nvme0n1"))


class ApstSupportedTests(unittest.TestCase):
    def test_apsta_bit_set(self):
        with mock.patch("utils.tuner.nvme_admin.admin_cmd",
                        side_effect=_identify_ctrl_result(0x01)) as m:
            self.assertTrue(_apst_supported("/dev/nvme0n1"))
            args, kwargs = m.call_args
            self.assertEqual(args[1], nvme_admin.OPC_IDENTIFY)
            self.assertEqual(kwargs.get("cdw10"), 1)

    def test_apsta_bit_clear(self):
        with mock.patch("utils.tuner.nvme_admin.admin_cmd",
                        side_effect=_identify_ctrl_result(0x00)):
            self.assertFalse(_apst_supported("/dev/nvme0n1"))

    def test_identify_failure_returns_none(self):
        res = nvme_admin.AdminResult(False, 0, errno.EIO,
                                     "/dev/nvme0: Input/output error")

        def _fake(disk_path, opcode, cdw10=0, cdw11=0, nsid=0,
                  out_buf=None, timeout_ms=5000):
            return res

        with mock.patch("utils.tuner.nvme_admin.admin_cmd", side_effect=_fake):
            self.assertIsNone(_apst_supported("/dev/nvme0n1"))


class ApplyGovernorTests(unittest.TestCase):
    def _tuner(self, disks=None):
        return SystemTuner(disks or TARGET_NVME)

    def test_no_governor_paths_not_critical(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._all_governor_paths", return_value=[]):
            tuner._apply_cpu_governor()
        self.assertTrue(tuner.governor_failed)
        self.assertEqual(len(tuner.applied), 1)
        self.assertFalse(tuner.applied[0]["success"])

    def test_write_and_verify_ok(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.read_text.return_value = "performance"
        with mock.patch("utils.tuner._all_governor_paths", return_value=[fake_p]), \
             mock.patch.object(Path, "write_text"):
            tuner._apply_cpu_governor()
        self.assertFalse(tuner.governor_failed)
        self.assertEqual(len(tuner.applied), 1)
        self.assertTrue(tuner.applied[0]["success"])

    def test_verify_fails_not_critical(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.parent.parent.name = "cpu0"
        fake_p.read_text.return_value = "powersave"
        with mock.patch("utils.tuner._all_governor_paths", return_value=[fake_p]), \
             mock.patch.object(Path, "write_text"):
            tuner._apply_cpu_governor()
        self.assertTrue(tuner.governor_failed)
        self.assertEqual(len(tuner.applied), 1)
        self.assertFalse(tuner.applied[0]["success"])

    def test_write_oserror_not_critical(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.write_text.side_effect = OSError("permission denied")
        with mock.patch("utils.tuner._all_governor_paths", return_value=[fake_p]):
            tuner._apply_cpu_governor()
        self.assertTrue(tuner.governor_failed)
        self.assertEqual(len(tuner.applied), 1)
        self.assertFalse(tuner.applied[0]["success"])

    def test_multiple_cpus_all_verified(self):
        tuner = self._tuner()
        p0 = mock.Mock()
        p1 = mock.Mock()
        p0.read_text.return_value = "performance"
        p1.read_text.return_value = "performance"
        with mock.patch("utils.tuner._all_governor_paths", return_value=[p0, p1]), \
             mock.patch.object(Path, "write_text"):
            tuner._apply_cpu_governor()
        self.assertEqual(len(tuner.applied), 1)
        self.assertTrue(tuner.applied[0]["success"])


class ApplyApstTests(unittest.TestCase):
    def _tuner(self, disks=None):
        return SystemTuner(disks or TARGET_NVME)

    def test_apst_not_supported_skipped_neutral(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=False), \
             mock.patch("utils.tuner._set_apst") as mock_set:
            tuner._apply_nvme_apst()
        mock_set.assert_not_called()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertEqual(entry["target_disks"], "nvme0n1")
        self.assertTrue(entry["success"])
        self.assertEqual(entry["after"], "не поддерживается")
        self.assertNotIn("error", entry)

    def test_ctrl_unavailable_recorded_as_failure(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=None), \
             mock.patch("utils.tuner._set_apst") as mock_set:
            tuner._apply_nvme_apst()
        mock_set.assert_not_called()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertEqual(entry["param"], "NVMe APST")
        self.assertEqual(entry["target_disks"], "nvme0n1")
        self.assertFalse(entry["success"])
        self.assertIn("контроллер недоступен", entry["error"])

    def test_apst_invalid_field_treated_as_unsupported(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=True), \
             mock.patch("utils.tuner._set_apst",
                        return_value=(False, True, "/dev/nvme0: Invalid argument")), \
             mock.patch("utils.tuner._read_apst") as mock_read:
            tuner._apply_nvme_apst()
        mock_read.assert_not_called()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertTrue(entry["success"])
        self.assertEqual(entry["after"], "не поддерживается")

    def test_apst_already_disabled_recorded(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=True), \
             mock.patch("utils.tuner._set_apst", return_value=(True, False, "")), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertEqual(entry["target_disks"], "nvme0n1")
        self.assertTrue(entry["success"])

    def test_apst_disable_ok(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=True), \
             mock.patch("utils.tuner._set_apst", return_value=(True, False, "")), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        self.assertTrue(tuner.applied[0]["success"])

    def test_apst_set_feature_fails_reports_error(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=True), \
             mock.patch("utils.tuner._set_apst",
                        return_value=(False, False,
                                      "/dev/nvme0: feature not changeable")), \
             mock.patch("utils.tuner._read_apst", return_value="enabled"):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertFalse(entry["success"])
        self.assertIn("feature not changeable", entry["error"])

    def test_apst_set_feature_ok_but_still_enabled(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._apst_supported", return_value=True), \
             mock.patch("utils.tuner._set_apst", return_value=(True, False, "")), \
             mock.patch("utils.tuner._read_apst", return_value="enabled"):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertFalse(entry["success"])
        self.assertEqual(entry["error"], "APST не отключился (проверка по чтению)")

    def test_no_nvme_disks(self):
        tuner = self._tuner(disks=TARGET_SATA)
        tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 0)


class NumaCpusTests(unittest.TestCase):
    def test_valid_cpulist(self):
        disk = [{"name": "nvme0n1", "tran": "NVME", "numa_node": 1}]
        tuner = SystemTuner(disk)
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "0-11,24-35"
        with mock.patch("utils.tuner.Path", return_value=fake_path):
            result = tuner.get_numa_cpus("nvme0n1")
        self.assertEqual(result, "0-11,24-35")

    def test_no_numa_node(self):
        disk = [{"name": "nvme0n1", "tran": "NVME", "numa_node": -1}]
        tuner = SystemTuner(disk)
        self.assertIsNone(tuner.get_numa_cpus("nvme0n1"))

    def test_missing_numa_key(self):
        disk = [{"name": "nvme0n1", "tran": "NVME"}]
        tuner = SystemTuner(disk)
        self.assertIsNone(tuner.get_numa_cpus("nvme0n1"))

    def test_path_not_exists(self):
        disk = [{"name": "nvme0n1", "tran": "NVME", "numa_node": 1}]
        tuner = SystemTuner(disk)
        fake_path = mock.Mock()
        fake_path.exists.return_value = False
        with mock.patch("utils.tuner.Path", return_value=fake_path):
            self.assertIsNone(tuner.get_numa_cpus("nvme0n1"))

    def test_unknown_disk(self):
        tuner = SystemTuner([])
        self.assertIsNone(tuner.get_numa_cpus("nvme0n1"))

    def test_invalid_cpulist_chars(self):
        disk = [{"name": "nvme0n1", "tran": "NVME", "numa_node": 1}]
        tuner = SystemTuner(disk)
        fake_path = mock.Mock()
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "abc"
        with mock.patch("utils.tuner.Path", return_value=fake_path):
            self.assertIsNone(tuner.get_numa_cpus("nvme0n1"))


class NvmeTempsTests(unittest.TestCase):
    def test_read_temp(self):
        tmp = Path(tempfile.mkdtemp())
        hwmon = tmp / "sys" / "class" / "nvme" / "nvme0" / "hwmon" / "hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "temp1_input").write_text("42000")

        root = str(tmp)

        def _mapped(path):
            p = str(path)
            if p.startswith("/sys/class/nvme"):
                return Path(root) / "sys" / p[len("/sys"):].lstrip("/")
            return Path(p)

        with mock.patch("utils.tuner.Path", side_effect=_mapped):
            tuner = SystemTuner([])
            temps = tuner.get_nvme_temps()
        self.assertIn("nvme0", temps)
        self.assertAlmostEqual(temps["nvme0"], 42.0)

    def test_no_hwmon(self):
        tuner = SystemTuner([])
        with mock.patch("utils.tuner.Path") as MockPath:
            fake_ctrl = mock.Mock()
            fake_ctrl.is_dir.return_value = True
            fake_ctrl.__truediv__ = lambda self, x: mock.Mock(exists=lambda: False, is_dir=lambda: False)
            MockPath.return_value.glob.return_value = [fake_ctrl]
            temps = tuner.get_nvme_temps()
        self.assertIsInstance(temps, dict)


class PrintSummaryTests(unittest.TestCase):
    def test_empty_applied(self):
        tuner = SystemTuner([])
        tuner.applied = []
        tuner.print_summary()

    def test_success_and_failure(self):
        tuner = SystemTuner([])
        tuner.applied = [
            {"param": "governor", "after": "y", "success": True},
            {"param": "APST", "after": "b", "success": False, "error": "fail"},
        ]
        with mock.patch("utils.tuner.console"):
            tuner.print_summary()


class ReportTests(unittest.TestCase):
    def test_returns_applied(self):
        tuner = SystemTuner([])
        tuner.applied = [{"param": "test"}]
        self.assertEqual(tuner.report(), [{"param": "test"}])


class ApplyIntegrationTests(unittest.TestCase):
    def test_apply_calls_both(self):
        tuner = SystemTuner(TARGET_NVME)
        with mock.patch.object(tuner, "_apply_cpu_governor") as mock_gov, \
             mock.patch.object(tuner, "_apply_nvme_apst") as mock_apst:
            tuner.apply()
        mock_gov.assert_called_once()
        mock_apst.assert_called_once()
