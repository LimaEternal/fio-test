import json
import queue
import sys
import threading
import time
import unittest
from importlib import util
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# fio-test.py содержит дефис в имени — импортируем через spec
_spec = util.spec_from_file_location("fio_test", PROJECT_ROOT / "fio-test.py")
fio_test = util.module_from_spec(_spec)
_spec.loader.exec_module(fio_test)


DISK = {
    "name": "nvme0n1", "path": "/dev/nvme0n1", "model": "KIOXIA KCMY1VUG3T20",
    "serial": "SN", "tran": "NVME", "size": "3.2T", "phy_sec": 4096,
    "slot": "nvme0", "pcie_info": {"gen": 5, "width": 4, "speed_gts": 32.0},
    "root_partition": None,
}

NVME_TEST_IDS = ["seq_read", "seq_write", "rand_read", "rand_write"]


class RunDiskTestsTests(unittest.TestCase):
    def test_tests_of_one_disk_run_in_order(self):
        plan = [(t, ["--rw=read"]) for t in NVME_TEST_IDS]
        results = [{"_thresholds": {}}]
        call_log = []

        def fake_run(disk, test_id, fio_args, cancel_event=None, diag_store=None,
                     tuner=None, state_lock=None, live_store=None):
            call_log.append(test_id)
            return {"iops": 1, "bw_mb": 1, "lat_avg": 0.1, "lat_p99": 0.2}

        with mock.patch.object(fio_test, "run_fio_test", side_effect=fake_run):
            fio_test.run_disk_tests(0, DISK, plan, results)

        self.assertEqual(call_log, NVME_TEST_IDS)
        self.assertEqual(
            set(results[0]) - {"_thresholds"}, set(NVME_TEST_IDS)
        )

    def test_error_result_does_not_break_following_tests(self):
        plan = [
            ("seq_read", ["--rw=read"]),
            ("seq_write", ["--rw=write"]),
        ]
        results = [{"_thresholds": {}}]
        call_log = []

        def fake_run(disk, test_id, fio_args, cancel_event=None, diag_store=None,
                     tuner=None, state_lock=None, live_store=None):
            call_log.append(test_id)
            if test_id == "seq_read":
                return {"error": "fio не найден"}
            return {"iops": 1, "bw_mb": 1, "lat_avg": 0.1, "lat_p99": 0.2}

        with mock.patch.object(fio_test, "run_fio_test", side_effect=fake_run):
            fio_test.run_disk_tests(0, DISK, plan, results)

        self.assertEqual(call_log, ["seq_read", "seq_write"])
        self.assertIn("error", results[0]["seq_read"])
        self.assertIn("seq_write", results[0])


class ParseFioResultTests(unittest.TestCase):
    def test_deep_fields_parsed_from_fio_json(self):
        raw = json.dumps({
            "jobs": [{
                "read": {
                    "bw_bytes": 1500000000,
                    "iops": 500000,
                    "io_kbytes": 2000000,
                    "lat_ns": {"mean": 100000, "percentile": {
                        "50.000000": 50000, "90.000000": 90000,
                        "99.000000": 120000, "99.900000": 200000}},
                    "slat_ns": {"mean": 5000},
                },
                "usage": {"user": 10.5, "sys": 5.2},
                "iodepth_level": {"1": 0, "2": 0, "4": 0, "8": 0, "16": 100},
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)

        self.assertEqual(res["iops"], 500000)
        self.assertAlmostEqual(res["bw_mb"], 1500000000 / (1024 * 1024), places=1)
        self.assertAlmostEqual(res["lat_avg"], 0.1, places=3)
        self.assertEqual(res["cpu_user"], 10.5)
        self.assertEqual(res["cpu_sys"], 5.2)
        self.assertEqual(res["clat_p50_ms"], 0.05)
        self.assertEqual(res["clat_p99_ms"], 0.12)
        self.assertEqual(res["clat_p99_9_ms"], 0.2)
        self.assertEqual(res["slat_avg_ms"], 0.005)
        self.assertEqual(res["iodepth"], 16)
        self.assertEqual(res["io_kb"], 2000000)

    def test_write_mode_selected_by_test_id(self):
        raw = json.dumps({
            "jobs": [{
                "write": {"bw_bytes": 1000, "iops": 10},
                "read": {"bw_bytes": 999999999, "iops": 999},
            }]
        })
        res = fio_test._parse_fio_result("seq_write", raw)
        self.assertEqual(res["iops"], 10)

    def test_bad_json_returns_error(self):
        res = fio_test._parse_fio_result("seq_read", "not json {{{")
        self.assertIn("error", res)


class ParseArgsLoggingTests(unittest.TestCase):
    def test_logging_flag_parses(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-l"]):
            args = fio_test.parse_args()
        self.assertTrue(args.logging)

    def test_combined_short_flags_with_l(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-sl"]):
            args = fio_test.parse_args()
        self.assertTrue(args.sequential)
        self.assertTrue(args.logging)


class MainParallelModeTests(unittest.TestCase):
    """Параллельный режим должен отправлять в пул по одной задаче на диск."""

    def test_parallel_mode_submits_one_task_per_disk(self):
        disks = [dict(DISK, name="nvme0n1", slot="nvme0"),
                 dict(DISK, name="nvme1n1", slot="nvme1")]

        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py"]), \
             mock.patch.object(fio_test, "_default_report_path",
                               return_value=Path("reports") / "t.md"), \
             mock.patch.object(fio_test, "run_disk_tests") as fake_runner:
            fio_test.main()

        self.assertEqual(fake_runner.call_count, 2)
        for call in fake_runner.call_args_list:
            args, kwargs = call
            _, disk, plan, _ = args
            self.assertEqual([t for t, _ in plan], NVME_TEST_IDS)
            self.assertEqual(kwargs.get("cancel_event") is not None, True)

    def test_logging_mode_passes_diag_store_to_runner_and_report(self):
        disks = [dict(DISK, name="nvme0n1", slot="nvme0")]
        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md") as fake_report, \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-l"]), \
             mock.patch.object(fio_test, "collect_static_info",
                               return_value={"numa_node": None, "cpu_affinity": None}), \
             mock.patch.object(fio_test, "_default_report_path",
                               return_value=Path("reports") / "t.md"), \
             mock.patch.object(fio_test, "run_disk_tests") as fake_runner:
            fio_test.main()

        self.assertEqual(fake_runner.call_count, 1)
        _, kwargs = fake_runner.call_args
        self.assertIsNotNone(kwargs.get("diag_store"))
        _, report_kwargs = fake_report.call_args
        self.assertIsNotNone(report_kwargs.get("diag_store"))
        self.assertTrue(report_kwargs.get("show_lat_p99"))

    def test_non_logging_mode_passes_none_diag_store(self):
        disks = [dict(DISK, name="nvme0n1", slot="nvme0")]
        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md") as fake_report, \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py"]), \
             mock.patch.object(fio_test, "_default_report_path",
                               return_value=Path("reports") / "t.md"), \
             mock.patch.object(fio_test, "run_disk_tests") as fake_runner:
            fio_test.main()

        _, kwargs = fake_runner.call_args
        self.assertIsNone(kwargs.get("diag_store"))
        _, report_kwargs = fake_report.call_args
        self.assertIsNone(report_kwargs.get("diag_store"))
        self.assertFalse(report_kwargs.get("show_lat_p99"))
        self.assertIsNotNone(report_kwargs.get("run_info"))
        self.assertIsNotNone(report_kwargs.get("fio_configs"))


class RunFioTestDiagStoreTests(unittest.TestCase):
    """run_fio_test в диагностическом режиме заполняет diag_store сэмплами."""

    def test_diag_store_filled_with_samples_and_summary(self):
        raw = json.dumps({
            "jobs": [{
                "read": {"bw_bytes": 1000, "iops": 1},
                "usage": {"user": 0.1, "sys": 0.2},
            }]
        })
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        diag_store = {}
        samples = [{
            "gts": 32.0, "width": 4, "temp": 41.0,
            "read_mbs": 1000.0, "write_mbs": 0.0, "iops": 100, "avgqu_sz": 10.0,
        }]
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            fake_sampler = fake_sampler_cls.return_value
            fake_sampler.samples = samples
            fake_sampler.summary.return_value = {
                "link_gts_min": 32.0, "link_width_min": 4, "temp_max_c": 41.0,
                "read_mbs_avg": 1000.0, "write_mbs_avg": 0.0, "iops_avg": 100,
                "avgqu_sz_max": 10.0, "samples": 1,
            }
            res = fio_test.run_fio_test(
                DISK, "seq_read", ["--rw=read"], diag_store=diag_store
            )

        self.assertIn("diag", res)
        self.assertIn("nvme0n1", diag_store)
        self.assertIn("seq_read", diag_store["nvme0n1"])
        self.assertEqual(
            diag_store["nvme0n1"]["seq_read"]["samples"], samples
        )
        self.assertEqual(
            diag_store["nvme0n1"]["seq_read"]["summary"]["temp_max_c"], 41.0
        )

    def test_no_diag_store_means_no_sampler(self):
        raw = json.dumps({"jobs": [{"read": {"bw_bytes": 1000, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            res = fio_test.run_fio_test(DISK, "seq_read", ["--rw=read"])

        self.assertNotIn("diag", res)
        fake_sampler_cls.assert_not_called()


class MaxIodepthOverflowTests(unittest.TestCase):
    """fio помечает переполненную корзину гистограммы глубины как ">=64"."""

    def test_max_iodepth_handles_overflow_bucket(self):
        self.assertEqual(
            fio_test._max_iodepth({"16": 0, "32": 100, ">=64": 1}), 64
        )

    def test_max_iodepth_ignores_keys_without_digits(self):
        self.assertEqual(fio_test._max_iodepth({"16": 100, "extra": 5}), 16)

    def test_max_iodepth_empty(self):
        self.assertIsNone(fio_test._max_iodepth({}))

    def test_parse_fio_result_with_overflow_bucket(self):
        raw = json.dumps({
            "jobs": [{
                "read": {"bw_bytes": 1000, "iops": 1},
                "iodepth_level": {"1": 0, "2": 0, "4": 0, "8": 0, "16": 100, ">=64": 1},
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertEqual(res["iodepth"], 64)


class RunFioTestParseWrapTests(unittest.TestCase):
    """Сбой разбора результата одного теста не должен ронять весь прогон."""

    def test_parse_error_returns_error_dict(self):
        fake = (mock.Mock(returncode=0), b"{}", b"")
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "_parse_fio_result",
                               side_effect=ValueError("boom")):
            res = fio_test.run_fio_test(DISK, "seq_read", ["--rw=read"])

        self.assertIn("error", res)
        self.assertIn("boom", res["error"])


class RunDiskTestsReportQueueTests(unittest.TestCase):
    """После каждого теста run_disk_tests уведомляет writer об обновлении отчёта."""

    def test_puts_marker_after_each_test(self):
        plan = [(t, ["--rw=read"]) for t in NVME_TEST_IDS]
        results = [{"_thresholds": {}}]
        q = queue.Queue()

        def fake_run(disk, test_id, fio_args, cancel_event=None, diag_store=None,
                     tuner=None, state_lock=None, live_store=None):
            return {"iops": 1, "bw_mb": 1, "lat_avg": 0.1, "lat_p99": 0.2}

        with mock.patch.object(fio_test, "run_fio_test", side_effect=fake_run):
            fio_test.run_disk_tests(0, DISK, plan, results, report_queue=q)

        self.assertEqual(q.qsize(), len(NVME_TEST_IDS))

    def test_no_queue_no_markers(self):
        plan = [("seq_read", ["--rw=read"])]
        results = [{"_thresholds": {}}]

        def fake_run(disk, test_id, fio_args, cancel_event=None, diag_store=None,
                     tuner=None, state_lock=None, live_store=None):
            return {"iops": 1, "bw_mb": 1, "lat_avg": 0.1, "lat_p99": 0.2}

        with mock.patch.object(fio_test, "run_fio_test", side_effect=fake_run):
            fio_test.run_disk_tests(0, DISK, plan, results)

        self.assertIn("seq_read", results[0])


class SnapshotStateTests(unittest.TestCase):
    def test_returns_copies_of_results(self):
        results = [{"_thresholds": {}, "seq_read": {"iops": 1}}]
        snap, _ = fio_test._snapshot_state(results, None, None, threading.Lock())
        results[0]["seq_read"] = {"iops": 999}
        self.assertEqual(snap[0]["seq_read"]["iops"], 1)

    def test_preserves_none_diag_store(self):
        _, diag = fio_test._snapshot_state([{}], None, None, threading.Lock())
        self.assertIsNone(diag)

    def test_merges_live_entries_into_diag_snapshot(self):
        lock = threading.Lock()
        samples = [{"temp": 41.0}]
        live = {"nvme0n1": {"test_id": "seq_read", "samples": samples}}
        _, diag = fio_test._snapshot_state([{}], {}, live, lock)
        entry = diag["nvme0n1"]["seq_read"]
        self.assertEqual(entry["samples"], samples)
        self.assertEqual(entry["summary"], {})


class ReportWriterTests(unittest.TestCase):
    def _start(self, has_live):
        q = queue.Queue()
        events = []
        writer = fio_test._ReportWriter(
            q, lambda: events.append("render"), has_live, tick=0.05
        )
        writer.start()
        return q, events, writer

    def test_renders_on_notification_and_stops(self):
        q, events, writer = self._start(lambda: False)
        q.put(0)
        q.put(1)
        time.sleep(0.2)
        q.put(fio_test._STOP)
        writer.join(timeout=2)
        self.assertFalse(writer.is_alive())
        self.assertTrue(any(e == "render" for e in events))

    def test_renders_on_live_tick_but_not_when_idle(self):
        active = {"flag": False}

        q, events, writer = self._start(lambda: active["flag"])
        time.sleep(0.15)
        self.assertEqual(events, [])
        active["flag"] = True
        time.sleep(0.15)
        q.put(fio_test._STOP)
        writer.join(timeout=2)
        self.assertTrue(any(e == "render" for e in events))


class MainIncrementalReportTests(unittest.TestCase):
    """С -l отчёт пишется до запуска тестов и ещё раз в конце прогона."""

    def test_logging_mode_writes_initial_report_before_tests(self):
        disks = [dict(DISK, name="nvme0n1", slot="nvme0")]
        order = []

        def record_report(*args, **kwargs):
            order.append("report")
            return "rep.md"

        def record_runner(*args, **kwargs):
            order.append("runner")

        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "generate_report", side_effect=record_report), \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-l"]), \
             mock.patch.object(fio_test, "collect_static_info",
                               return_value={"numa_node": None, "cpu_affinity": None}), \
             mock.patch.object(fio_test, "_default_report_path",
                               return_value=Path("reports") / "t.md"), \
             mock.patch.object(fio_test, "run_disk_tests", side_effect=record_runner):
            fio_test.main()

        self.assertEqual(order[0], "report")
        self.assertGreaterEqual(order.count("report"), 2)


if __name__ == "__main__":
    unittest.main()
