import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.tuner import SystemTuner, _governor_path, _all_governor_paths, _read_apst

TARGET_NVME = [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "tran": "NVME"},
]
TARGET_SATA = [
    {"name": "sda", "path": "/dev/sda", "tran": "SATA"},
]


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
        with mock.patch("utils.tuner.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="NVMe APST: Enabled\n",
            )
            result = _read_apst("/dev/nvme0n1")
            self.assertEqual(result, "enabled")

    def test_disabled(self):
        with mock.patch("utils.tuner.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="NVMe APST: Disabled\n",
            )
            result = _read_apst("/dev/nvme0n1")
            self.assertEqual(result, "disabled")

    def test_nonzero_returncode(self):
        with mock.patch("utils.tuner.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="")
            result = _read_apst("/dev/nvme0n1")
            self.assertIsNone(result)

    def test_nvme_cli_missing(self):
        with mock.patch("utils.tuner.subprocess.run", side_effect=FileNotFoundError):
            result = _read_apst("/dev/nvme0n1")
            self.assertIsNone(result)


class ApplyGovernorTests(unittest.TestCase):
    def _tuner(self, disks=None):
        return SystemTuner(disks or TARGET_NVME)

    def test_no_governor_paths_exits(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._all_governor_paths", return_value=[]), \
             self.assertRaises(SystemExit):
            tuner._apply_cpu_governor()

    def test_write_and_verify_ok(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.read_text.return_value = "performance"
        with mock.patch("utils.tuner._all_governor_paths", return_value=[fake_p]), \
             mock.patch.object(Path, "write_text"):
            tuner._apply_cpu_governor()
        self.assertEqual(len(tuner.applied), 1)
        self.assertTrue(tuner.applied[0]["success"])

    def test_verify_fails_exits(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.parent.parent.name = "cpu0"
        fake_p.read_text.return_value = "powersave"
        with mock.patch("utils.tuner._all_governor_paths", return_value=[fake_p]), \
             mock.patch.object(Path, "write_text"), \
             self.assertRaises(SystemExit):
            tuner._apply_cpu_governor()

    def test_write_oserror_exits(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.write_text.side_effect = OSError("permission denied")
        with mock.patch("utils.tuner._all_governor_paths", return_value=[fake_p]), \
             self.assertRaises(SystemExit):
            tuner._apply_cpu_governor()

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

    def test_nvme_cli_missing_recorded(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner.subprocess.run", side_effect=FileNotFoundError):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertEqual(entry["param"], "NVMe APST")
        self.assertEqual(entry["target_disks"], "nvme0n1")
        self.assertFalse(entry["success"])
        self.assertTrue(entry["error"])

    def test_apst_already_disabled_recorded(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner.subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        entry = tuner.applied[0]
        self.assertEqual(entry["target_disks"], "nvme0n1")
        self.assertTrue(entry["success"])

    def test_apst_disable_ok(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._read_apst", return_value="disabled"), \
             mock.patch("utils.tuner.subprocess.run", return_value=mock.Mock(returncode=0)):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        self.assertTrue(tuner.applied[0]["success"])

    def test_apst_disable_fails(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._read_apst", side_effect=["enabled", "enabled"]), \
             mock.patch("utils.tuner.subprocess.run", return_value=mock.Mock(returncode=1)):
            tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 1)
        self.assertFalse(tuner.applied[0]["success"])

    def test_no_nvme_disks(self):
        tuner = self._tuner(disks=TARGET_SATA)
        tuner._apply_nvme_apst()
        self.assertEqual(len(tuner.applied), 0)


class PreviewTests(unittest.TestCase):
    def _tuner(self, disks=None):
        return SystemTuner(disks or TARGET_NVME)

    def test_governor_not_performance(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.exists.return_value = True
        fake_p.read_text.return_value = "powersave"
        with mock.patch("utils.tuner._governor_path", return_value=fake_p), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            rows = tuner.preview()
        params = [r["param"] for r in rows]
        self.assertIn("CPU governor", params)

    def test_governor_already_performance(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.exists.return_value = True
        fake_p.read_text.return_value = "performance"
        with mock.patch("utils.tuner._governor_path", return_value=fake_p), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            rows = tuner.preview()
        params = [r["param"] for r in rows]
        self.assertNotIn("CPU governor", params)

    def test_governor_path_missing(self):
        tuner = self._tuner()
        with mock.patch("utils.tuner._governor_path", return_value=None), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            rows = tuner.preview()
        params = [r["param"] for r in rows]
        self.assertIn("CPU governor", params)
        gov_row = [r for r in rows if r["param"] == "CPU governor"][0]
        self.assertIn("skipped_reason", gov_row)

    def test_apst_enabled_in_preview(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.exists.return_value = True
        fake_p.read_text.return_value = "performance"
        with mock.patch("utils.tuner._governor_path", return_value=fake_p), \
             mock.patch("utils.tuner._read_apst", return_value="enabled"):
            rows = tuner.preview()
        params = [r["param"] for r in rows]
        self.assertIn("NVMe APST", params)

    def test_apst_disabled_not_in_preview(self):
        tuner = self._tuner()
        fake_p = mock.Mock()
        fake_p.exists.return_value = True
        fake_p.read_text.return_value = "performance"
        with mock.patch("utils.tuner._governor_path", return_value=fake_p), \
             mock.patch("utils.tuner._read_apst", return_value="disabled"):
            rows = tuner.preview()
        params = [r["param"] for r in rows]
        self.assertNotIn("NVMe APST", params)


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
