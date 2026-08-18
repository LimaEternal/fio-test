import sys
import tempfile
import threading
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
    "slot": "nvme0", "root_partition": None,
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


class ResolveSizeBytesTests(unittest.TestCase):
    def test_zero_means_full_disk(self):
        self.assertEqual(prefill._resolve_size_bytes(0, 123456), 123456)

    def test_block_in_gib(self):
        disk = 3 * (2 ** 40)
        self.assertEqual(prefill._resolve_size_bytes(100, disk), 100 * (2 ** 30))

    def test_small_disk_wins(self):
        disk = 50 * (2 ** 30)
        self.assertEqual(prefill._resolve_size_bytes(100, disk), disk)

    def test_none_disk_becomes_zero(self):
        self.assertEqual(prefill._resolve_size_bytes(100, None), 0)

    def test_zero_with_none_disk(self):
        self.assertEqual(prefill._resolve_size_bytes(0, None), 0)


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

    def _capture_cmd(self, config_args=None, block_gb=100):
        cmd_seen = {}

        def fake_stream(cmd, cancel_event, on_progress):
            cmd_seen["cmd"] = cmd
            return True

        with mock.patch.object(
            prefill, "_load_prefill_config",
            return_value=config_args if config_args is not None
            else ["--ioengine=io_uring", "--direct=1", "--bs=128k"],
        ), mock.patch.object(prefill, "_run_fio_stream", side_effect=fake_stream):
            ok = prefill.run_prefill(DISK, on_progress=lambda *a: None, block_gb=block_gb)
        self.assertTrue(ok)
        return cmd_seen["cmd"]

    def test_run_prefill_applies_default_block(self):
        cmd = self._capture_cmd(block_gb=100)
        self.assertIn("--size=100G", cmd)

    def test_run_prefill_custom_block(self):
        cmd = self._capture_cmd(block_gb=500)
        self.assertIn("--size=500G", cmd)
        self.assertNotIn("--size=100G", cmd)

    def test_run_prefill_overrides_config_size(self):
        cmd = self._capture_cmd(
            config_args=["--ioengine=io_uring", "--size=100%"], block_gb=100
        )
        self.assertIn("--size=100G", cmd)
        self.assertNotIn("--size=100%", cmd)

    def test_run_prefill_block_zero_keeps_config_size(self):
        cmd = self._capture_cmd(
            config_args=["--ioengine=io_uring", "--size=100%"], block_gb=0
        )
        self.assertIn("--size=100%", cmd)
        self.assertEqual(sum(1 for a in cmd if a.startswith("--size=")), 1)

    def test_run_prefill_block_zero_no_extra_size(self):
        cmd = self._capture_cmd(block_gb=0)
        self.assertFalse(any(a.startswith("--size=") for a in cmd))

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
    def test_prefill_disks_runs_all_disks(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        d2 = dict(DISK, name="nvme1n1", serial="S2")
        calls = []

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None,
                         block_gb=100):
            calls.append(disk["name"])
            return True

        with mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            prefill.prefill_disks([d1, d2])

        self.assertEqual(sorted(calls), ["nvme0n1", "nvme1n1"])

    def test_prefill_disks_returns_phase_duration(self):
        d1 = dict(DISK, name="nvme0n1", serial="S1")

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None,
                         block_gb=100):
            return True

        with mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            dur = prefill.prefill_disks([d1])

        self.assertIsInstance(dur, float)
        self.assertGreaterEqual(dur, 0)

    def _progress_total(self, block_gb, disk_bytes=4 * (2 ** 40)):
        d1 = dict(DISK, name="nvme0n1", serial="S1")
        progress_mock = mock.MagicMock()
        progress_mock.__enter__.return_value = progress_mock
        progress_mock.__exit__.return_value = False

        def fake_prefill(disk, cancel_event=None, tuner=None, on_progress=None,
                         block_gb=block_gb):
            return True

        with mock.patch.object(prefill, "_disk_total_bytes", return_value=disk_bytes), \
             mock.patch.object(prefill, "Progress", return_value=progress_mock), \
             mock.patch.object(prefill, "run_prefill", side_effect=fake_prefill):
            prefill.prefill_disks([d1], block_gb=block_gb)
        added = progress_mock.add_task.call_args_list[0]
        return added.kwargs["total"]

    def test_progress_total_capped_by_block(self):
        self.assertEqual(self._progress_total(100), 100 * (2 ** 30))

    def test_progress_total_full_disk_when_block_zero(self):
        self.assertEqual(self._progress_total(0), 4 * (2 ** 40))


PRETTY_STATUS = (
    '{\n'
    '  "fio version" : "fio-3.28",\n'
    '  "jobs" : [\n'
    '    {\n'
    '      "jobname" : "v",\n'
    '      "write" : {\n'
    '        "io_kbytes" : 2048,\n'
    '        "bw_bytes" : 1048576\n'
    '      }\n'
    '    }\n'
    '  ]\n'
    '}\n'
)


class ExtractStatusTests(unittest.TestCase):
    def test_pretty_multiline_status(self):
        statuses, rest = prefill._extract_fio_statuses(PRETTY_STATUS)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["jobs"][0]["write"]["io_kbytes"], 2048)
        self.assertFalse(rest.strip())

    def test_two_objects_concatenated(self):
        statuses, rest = prefill._extract_fio_statuses(PRETTY_STATUS * 2)
        self.assertEqual(len(statuses), 2)
        self.assertFalse(rest.strip())

    def test_partial_status_kept_for_next_chunk(self):
        half = len(PRETTY_STATUS) // 2
        statuses, rest = prefill._extract_fio_statuses(PRETTY_STATUS[:half])
        self.assertEqual(statuses, [])
        self.assertNotEqual(rest, "")
        statuses, rest = prefill._extract_fio_statuses(rest + PRETTY_STATUS[half:])
        self.assertEqual(len(statuses), 1)
        self.assertFalse(rest.strip())

    def test_mixed_text_around_status(self):
        text = "fio: looks like your fs does not support direct=1\n" + PRETTY_STATUS + "Jobs: 1 (f=1)\n"
        statuses, rest = prefill._extract_fio_statuses(text)
        self.assertEqual(len(statuses), 1)

    def test_braces_inside_strings_ignored(self):
        text = '{\n  "a" : "}}",\n  "b" : { "c" : "{" },\n  "io_kbytes" : 7\n}\n'
        statuses, rest = prefill._extract_fio_statuses(text)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["b"]["c"], "{")
        self.assertEqual(statuses[0]["io_kbytes"], 7)

    def test_no_braces_returns_empty(self):
        statuses, rest = prefill._extract_fio_statuses("no json here\n")
        self.assertEqual(statuses, [])
        self.assertEqual(rest, "no json here\n")


class FioStreamTests(unittest.TestCase):
    def _make_proc(self):
        stdout = mock.Mock()
        stdout.fileno.return_value = 11
        stderr = mock.Mock()
        stderr.fileno.return_value = 22
        proc = mock.Mock()
        proc.stdout = stdout
        proc.stderr = stderr
        proc.pid = 12345
        return proc

    def test_feeds_progress_from_pretty_json_and_succeeds(self):
        proc = self._make_proc()
        read_plan = {11: [PRETTY_STATUS.encode(), b""], 22: [b""]}
        selects = [
            ([proc.stdout, proc.stderr], [], []),
            ([proc.stdout], [], []),
            ([], [], []),
        ]

        def fake_read(fd, n):
            q = read_plan[fd]
            return q.pop(0) if q else b""

        def fake_select(readable, writable, exc, timeout):
            return selects.pop(0)

        proc.poll.side_effect = [None, None, 0]
        proc.wait.return_value = 0
        seen = []
        with mock.patch.object(prefill.subprocess, "Popen", return_value=proc), \
             mock.patch.object(prefill.select, "select", side_effect=fake_select), \
             mock.patch.object(prefill.os, "read", side_effect=fake_read), \
             mock.patch.object(prefill, "_kill_tree") as kill:
            result = prefill._run_fio_stream(
                ["fio", "--x"], on_progress=lambda i, b: seen.append((i, b))
            )
        self.assertIs(result, True)
        self.assertEqual(seen[-1][0], 2048 * 1024)
        self.assertEqual(seen[-1][1], 1048576)

    def test_stall_when_statuses_show_zero_bytes(self):
        proc = self._make_proc()
        zero_status = PRETTY_STATUS.replace("2048", "0").encode()

        def fake_read(fd, n):
            return zero_status

        def fake_select(readable, writable, exc, timeout):
            return [proc.stdout], [], []

        clock = [100.0]

        def fake_mono():
            clock[0] += 1
            return clock[0]

        proc.poll.return_value = None
        with mock.patch.object(prefill.subprocess, "Popen", return_value=proc), \
             mock.patch.object(prefill.select, "select", side_effect=fake_select), \
             mock.patch.object(prefill.os, "read", side_effect=fake_read), \
             mock.patch.object(prefill, "_kill_tree") as kill, \
             mock.patch.object(prefill.time, "monotonic", side_effect=fake_mono):
            result = prefill._run_fio_stream(["fio", "--x"])
        self.assertEqual(result, "stall")
        kill.assert_called_once_with(proc)

    def test_stall_when_no_output_at_all(self):
        proc = self._make_proc()

        def fake_select(readable, writable, exc, timeout):
            return [], [], []

        clock = [100.0]

        def fake_mono():
            clock[0] += 1
            return clock[0]

        proc.poll.return_value = None
        with mock.patch.object(prefill.subprocess, "Popen", return_value=proc), \
             mock.patch.object(prefill.select, "select", side_effect=fake_select), \
             mock.patch.object(prefill, "_kill_tree") as kill, \
             mock.patch.object(prefill.time, "monotonic", side_effect=fake_mono):
            result = prefill._run_fio_stream(["fio", "--x"])
        self.assertEqual(result, "stall")
        kill.assert_called_once_with(proc)

    def test_cancel_returns_none(self):
        proc = self._make_proc()
        cancel = threading.Event()
        cancel.set()

        def fake_select(readable, writable, exc, timeout):
            return [], [], []

        with mock.patch.object(prefill.subprocess, "Popen", return_value=proc), \
             mock.patch.object(prefill.select, "select", side_effect=fake_select), \
             mock.patch.object(prefill, "_kill_tree") as kill:
            result = prefill._run_fio_stream(["fio", "--x"], cancel_event=cancel)
        self.assertIsNone(result)
        kill.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()
