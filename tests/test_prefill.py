import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import utils.format as format_mod
import utils.prefill as prefill


DISK = {
    "name": "nvme0n1", "path": "/dev/nvme0n1", "model": "KIOXIA KCMY1VUG3T20",
    "serial": "SN", "tran": "NVME", "size": "3.2T", "phy_sec": 4096,
    "slot": "nvme0", "pcie_info": {"gen": 5, "width": 4, "speed_gts": 32.0},
    "root_partition": None,
}


class FormatDurationTests(unittest.TestCase):
    def test_duration_seconds(self):
        self.assertEqual(format_mod.format_duration(42), "42 с")

    def test_duration_minutes(self):
        self.assertEqual(format_mod.format_duration(125), "2 мин 05 с")

    def test_duration_hours(self):
        self.assertEqual(format_mod.format_duration(3725), "1 ч 02 мин")

    def test_duration_rounds(self):
        self.assertEqual(format_mod.format_duration(42.4), "42 с")


class FormatMiscTests(unittest.TestCase):
    def test_bytes_units(self):
        self.assertEqual(format_mod.format_bytes(1536), "1.5КБ")
        self.assertEqual(format_mod.format_bytes(1024 * 1024), "1.0МБ")

    def test_bw_zero(self):
        self.assertEqual(format_mod.format_bw(0), "—")

    def test_bw_bytes(self):
        self.assertEqual(format_mod.format_bw(500), "500.0 Б/с")

    def test_bw_gibs(self):
        self.assertEqual(format_mod.format_bw(2147483648), "2.0 ГБ/с")


class PrefillConfigTests(unittest.TestCase):
    def test_load_config_parses_key_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "prefill.fio"
            cfg.write_text(
                "# comment\nioengine=psync\n\n; other comment\ndirect=1\n"
                "rw=write\nbs=128k\n",
                encoding="utf-8",
            )
            with mock.patch.object(prefill, "CONFIG_PATH", cfg):
                args = prefill._load_prefill_config()
        self.assertIn("--ioengine=psync", args)
        self.assertIn("--direct=1", args)
        self.assertIn("--rw=write", args)
        self.assertIn("--bs=128k", args)
        self.assertFalse(any("comment" in a for a in args))

    def test_load_config_defaults_engine_when_missing(self):
        with mock.patch.object(prefill, "CONFIG_PATH", Path("no-such-prefill.fio")):
            args = prefill._load_prefill_config()
        self.assertEqual(args, ["--ioengine=io_uring"])

    def test_load_config_appends_default_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "prefill.fio"
            cfg.write_text("direct=1\n", encoding="utf-8")
            with mock.patch.object(prefill, "CONFIG_PATH", cfg):
                args = prefill._load_prefill_config()
        self.assertIn("--ioengine=io_uring", args)


class PrefillStatsTests(unittest.TestCase):
    def test_extract_stats_from_write(self):
        status = {"jobs": [{"write": {"io_kbytes": 1024, "bw_bytes": 2097152}}]}
        self.assertEqual(prefill._extract_prefill_stats(status), (1048576, 2097152))

    def test_extract_stats_missing_write(self):
        self.assertEqual(
            prefill._extract_prefill_stats({"jobs": [{"read": {}}]}), (0, 0)
        )

    def test_extract_stats_empty_jobs(self):
        self.assertEqual(prefill._extract_prefill_stats({}), (0, 0))


class RunPrefillTests(unittest.TestCase):
    def test_run_prefill_builds_cmd_from_config(self):
        cmd_seen = {}

        def fake_stream(cmd, cancel_event, on_progress):
            cmd_seen["cmd"] = cmd
            cmd_seen["cb"] = on_progress
            return True

        with mock.patch.object(
            prefill, "_load_prefill_config",
            return_value=["--ioengine=io_uring", "--direct=1", "--bs=128k"],
        ), mock.patch.object(prefill, "_run_fio_stream", side_effect=fake_stream):
            ok = prefill.run_prefill(DISK, on_progress=lambda *a: None)

        self.assertTrue(ok)
        cmd = cmd_seen["cmd"]
        self.assertEqual(cmd[0], "fio")
        self.assertIn("--filename", cmd)
        self.assertIn("/dev/nvme0n1", cmd)
        self.assertIn("--ioengine=io_uring", cmd)
        self.assertIn("--direct=1", cmd)
        self.assertIn("--status-interval=1", cmd)
        self.assertIn("--output-format=json", cmd)
        self.assertNotIn("--cpus_allowed", cmd)
        self.assertIsNotNone(cmd_seen["cb"])

    def test_run_prefill_adds_numa_pinning(self):
        fake_tuner = mock.Mock()
        fake_tuner.get_numa_cpus.return_value = "0-11,24-35"
        cmd_seen = {}

        def fake_stream(cmd, cancel_event, on_progress):
            cmd_seen["cmd"] = cmd
            return True

        with mock.patch.object(
            prefill, "_load_prefill_config", return_value=["--ioengine=io_uring"]
        ), mock.patch.object(prefill, "_run_fio_stream", side_effect=fake_stream):
            ok = prefill.run_prefill(DISK, tuner=fake_tuner)

        self.assertTrue(ok)
        self.assertIn("--cpus_allowed", cmd_seen["cmd"])
        self.assertIn("0-11,24-35", cmd_seen["cmd"])

    def test_run_prefill_without_numa_omits_cpus_allowed(self):
        cmd_seen = {}

        def fake_stream(cmd, cancel_event, on_progress):
            cmd_seen["cmd"] = cmd
            return True

        with mock.patch.object(
            prefill, "_load_prefill_config", return_value=["--ioengine=io_uring"]
        ), mock.patch.object(prefill, "_run_fio_stream", side_effect=fake_stream):
            ok = prefill.run_prefill(DISK, tuner=None)

        self.assertTrue(ok)
        self.assertNotIn("--cpus_allowed", cmd_seen["cmd"])

    def test_run_prefill_falls_back_to_psync_once(self):
        calls = []

        def fake_stream(cmd, cancel_event, on_progress):
            calls.append(cmd)
            return "stall" if len(calls) == 1 else True

        with mock.patch.object(
            prefill, "_load_prefill_config",
            return_value=["--ioengine=io_uring", "--direct=1"],
        ), mock.patch.object(prefill, "_run_fio_stream", side_effect=fake_stream):
            ok = prefill.run_prefill(DISK)

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertIn("--ioengine=io_uring", calls[0])
        self.assertIn("--ioengine=psync", calls[1])
        self.assertNotIn("--ioengine=io_uring", calls[1])
        self.assertIn("--direct=1", calls[1])

    def test_run_prefill_no_infinite_fallback(self):
        with mock.patch.object(
            prefill, "_load_prefill_config", return_value=["--ioengine=io_uring"]
        ), mock.patch.object(prefill, "_run_fio_stream", return_value="stall") as stream:
            ok = prefill.run_prefill(DISK)
        self.assertFalse(ok)
        self.assertEqual(stream.call_count, 2)

    def test_run_prefill_cancel_returns_none(self):
        with mock.patch.object(
            prefill, "_load_prefill_config", return_value=["--ioengine=io_uring"]
        ), mock.patch.object(prefill, "_run_fio_stream", return_value=None) as stream:
            ok = prefill.run_prefill(DISK)
        self.assertIsNone(ok)
        self.assertEqual(stream.call_count, 1)


class PrefillDisksTests(unittest.TestCase):
    def test_prefill_disks_runs_all_and_saves_state(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        d2 = dict(DISK, name="nvme1n1", serial="S2")
        calls = []
        saved = {}

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None):
            calls.append(disk["name"])
            return True

        with mock.patch.object(prefill, "_load_prefill_state", return_value={}), \
             mock.patch.object(
                 prefill, "_save_prefill_state",
                 side_effect=lambda s: saved.update(s),
             ), mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            prefill.prefill_disks([d1, d2])

        self.assertEqual(sorted(calls), ["nvme0n1", "nvme1n1"])
        self.assertEqual(set(saved), {"S1", "S2"})
        self.assertEqual(saved["S1"]["size"], "3.2T")

    def test_prefill_disks_skips_already_filled(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        d2 = dict(DISK, name="nvme1n1", serial="S2")
        state = {"S1": {"model": "M", "size": "3.2T", "name": "nvme0n1"}}
        calls = []

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None):
            calls.append(disk["name"])
            return True

        with mock.patch.object(prefill, "_load_prefill_state", return_value=state), \
             mock.patch.object(prefill, "_save_prefill_state"), \
             mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            prefill.prefill_disks([d1, d2])

        self.assertEqual(calls, ["nvme1n1"])

    def test_prefill_disks_refills_when_size_changed(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        state = {"S1": {"model": "M", "size": "1.0T", "name": "nvme0n1"}}
        calls = []

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None):
            calls.append(disk["name"])
            return True

        with mock.patch.object(prefill, "_load_prefill_state", return_value=state), \
             mock.patch.object(prefill, "_save_prefill_state"), \
             mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            prefill.prefill_disks([d1])

        self.assertEqual(calls, ["nvme0n1"])

    def test_prefill_disks_failure_not_saved(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        d2 = dict(DISK, name="nvme1n1", serial="S2")
        saved = {}

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None):
            return disk["name"] == "nvme0n1"

        with mock.patch.object(prefill, "_load_prefill_state", return_value={}), \
             mock.patch.object(
                 prefill, "_save_prefill_state",
                 side_effect=lambda s: saved.update(s),
             ), mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            prefill.prefill_disks([d1, d2])

        self.assertEqual(set(saved), {"S1"})

    def test_prefill_disks_returns_phase_duration(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None):
            return True

        with mock.patch.object(prefill, "_load_prefill_state", return_value={}), \
             mock.patch.object(prefill, "_save_prefill_state"), \
             mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            dur = prefill.prefill_disks([d1])

        self.assertIsInstance(dur, float)
        self.assertGreaterEqual(dur, 0)

    def test_prefill_disks_returns_zero_when_nothing_to_fill(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        state = {"S1": {"model": "M", "size": "3.2T", "name": "nvme0n1"}}

        with mock.patch.object(prefill, "_load_prefill_state", return_value=state), \
             mock.patch.object(prefill, "_save_prefill_state"):
            dur = prefill.prefill_disks([d1])

        self.assertEqual(dur, 0.0)


if __name__ == "__main__":
    unittest.main()
