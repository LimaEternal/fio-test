import json
import queue
import sys
import tempfile
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
            set(results[0]) - {"_thresholds", "_wall_s"}, set(NVME_TEST_IDS)
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


class TestProgressConsoleTests(unittest.TestCase):
    """Консольный прогресс тестов (строки «Готово ...») — только в режиме -l."""

    RESULT = {"iops": 1000000, "bw_mb": 5800.4, "lat_avg": 0.82, "lat_p99": 1.6}

    def _run(self, diag_store):
        plan = [(t, ["--rw=read"]) for t in NVME_TEST_IDS]
        results = [{"_thresholds": {}}]
        printed = []

        def fake_console_print(*args, **kwargs):
            printed.append(args[0] if args else kwargs.get("text", ""))

        def fake_run(disk, test_id, fio_args, cancel_event=None, diag_store=None,
                     tuner=None, state_lock=None, live_store=None):
            return dict(self.RESULT)

        with mock.patch.object(fio_test, "run_fio_test", side_effect=fake_run), \
             mock.patch.object(fio_test, "console") as fake_console:
            fake_console.print.side_effect = fake_console_print
            fio_test.run_disk_tests(0, DISK, plan, results, diag_store=diag_store)
        return printed

    def test_logging_mode_prints_done_line_per_test(self):
        printed = self._run(diag_store={})
        done = [p for p in printed if "Готово" in str(p)]
        self.assertEqual(len(done), len(NVME_TEST_IDS))

    def test_non_logging_mode_prints_no_done_lines(self):
        printed = self._run(diag_store=None)
        self.assertFalse(any("Готово" in str(p) for p in printed))

    def test_format_test_done_contains_metrics(self):
        res = dict(self.RESULT, status="PASS")
        line = fio_test._format_test_done(DISK, "seq_read", res)
        self.assertIn("Готово /dev/nvme0n1", line)
        self.assertIn("Послед. чтение", line)
        self.assertIn("1,000,000 IOPS", line)
        self.assertIn("5800.4 МБ/с", line)
        self.assertIn("0.82 мс", line)
        self.assertIn("PASS", line)

    def test_format_test_done_fail_status(self):
        res = dict(self.RESULT, status="FAIL")
        self.assertIn("FAIL", fio_test._format_test_done(DISK, "seq_read", res))


class ParseFioResultTests(unittest.TestCase):
    def test_deep_fields_parsed_from_fio_json(self):
        raw = json.dumps({
            "jobs": [{
                "read": {
                    "bw_bytes": 1500000000,
                    "iops": 500000,
                    "io_kbytes": 2000000,
                    "lat_ns": {"mean": 100000},
                    "clat_ns": {"mean": 90000, "percentile": {
                        "50.000000": 50000, "90.000000": 90000,
                        "99.000000": 120000, "99.900000": 200000}},
                    "slat_ns": {"mean": 5000},
                },
                "usr_cpu": 10.5,
                "sys_cpu": 5.2,
                "iodepth_level": {"1": 0, "2": 0, "4": 0, "8": 0, "16": 100},
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)

        self.assertEqual(res["iops"], 500000)
        self.assertAlmostEqual(res["bw_mb"], 1500000000 / 1e6, places=1)
        self.assertAlmostEqual(res["lat_avg"], 0.1, places=3)
        self.assertAlmostEqual(res["lat_p99"], 0.12, places=3)
        self.assertEqual(res["cpu_user"], 10.5)
        self.assertEqual(res["cpu_sys"], 5.2)
        self.assertEqual(res["clat_p50_ms"], 0.05)
        self.assertEqual(res["clat_p99_ms"], 0.12)
        self.assertEqual(res["clat_p99_9_ms"], 0.2)
        self.assertEqual(res["slat_avg_ms"], 0.005)
        self.assertEqual(res["iodepth"], 16)
        self.assertEqual(res["io_kb"], 2000000)

    def test_cpu_usage_legacy_usage_key_fallback(self):
        raw = json.dumps({
            "jobs": [{
                "read": {"bw_bytes": 1000, "iops": 1},
                "usage": {"usr": 1.5, "sys": 2.5},
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertEqual(res["cpu_user"], 1.5)
        self.assertEqual(res["cpu_sys"], 2.5)

    def test_percentile_falls_back_to_lat_ns(self):
        raw = json.dumps({
            "jobs": [{
                "read": {
                    "bw_bytes": 1000, "iops": 1,
                    "lat_ns": {"mean": 100000, "percentile": {
                        "99.000000": 120000}},
                },
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertAlmostEqual(res["lat_p99"], 0.12, places=3)
        self.assertEqual(res["clat_p99_ms"], 0.12)

    def test_percentile_matched_by_numeric_key(self):
        raw = json.dumps({
            "jobs": [{
                "read": {
                    "bw_bytes": 1000, "iops": 1,
                    "clat_ns": {"percentile": {"99": 240000, "99.9": 300000}},
                },
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertAlmostEqual(res["lat_p99"], 0.24, places=3)
        self.assertEqual(res["clat_p99_ms"], 0.24)
        self.assertEqual(res["clat_p99_9_ms"], 0.3)

    def test_write_mode_selected_by_test_id(self):
        raw = json.dumps({
            "jobs": [{
                "write": {"bw_bytes": 1000, "iops": 10},
                "read": {"bw_bytes": 999999999, "iops": 999},
            }]
        })
        res = fio_test._parse_fio_result("seq_write", raw)
        self.assertEqual(res["iops"], 10)

    def test_p99_flagged_unreliable_when_far_above_avg(self):
        # Мусорный clat p99 ~17 с при avg ~0.6 мс (случай из реального отчёта).
        raw = json.dumps({
            "jobs": [{
                "read": {
                    "bw_bytes": 1000, "iops": 1,
                    "lat_ns": {"mean": 600000},
                    "clat_ns": {"percentile": {
                        "99.000000": 17112760000, "99.900000": 17112760000}},
                },
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertTrue(res["lat_p99_unreliable"])

    def test_p99_reliable_when_within_sane_range(self):
        raw = json.dumps({
            "jobs": [{
                "read": {
                    "bw_bytes": 1000, "iops": 1,
                    "lat_ns": {"mean": 100000},
                    "clat_ns": {"percentile": {"99.000000": 400000}},
                },
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertFalse(res["lat_p99_unreliable"])

    def test_p99_not_flagged_when_avg_missing(self):
        raw = json.dumps({
            "jobs": [{"read": {"bw_bytes": 1000, "iops": 1,
                                "clat_ns": {"percentile": {"99.000000": 17112760000}}}}]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertFalse(res["lat_p99_unreliable"])

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
                "usr_cpu": 0.1, "sys_cpu": 0.2,
            }]
        })
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        diag_store = {}
        samples = [{
            "gts": 32.0, "width": 4, "temp": 41.0,
            "read_mbs": 1000.0, "write_mbs": 0.0, "iops": 100,
        }]
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            fake_sampler = fake_sampler_cls.return_value
            fake_sampler.samples = samples
            fake_sampler.summary.return_value = {
                "link_gts_min": 32.0, "link_width_min": 4, "temp_max_c": 41.0,
                "read_mbs_avg": 1000.0, "write_mbs_avg": 0.0, "iops_avg": 100,
                "samples": 1,
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

    def test_raw_fio_json_saved_in_diag_mode(self):
        raw_text = json.dumps({"jobs": [{"read": {"bw_bytes": 1000, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw_text.encode(), b"")
        with tempfile.TemporaryDirectory() as tmp:
            real_path = Path(tmp)

            def fake_path(p):
                return real_path / p

            with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
                 mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls, \
                 mock.patch.object(fio_test, "Path", side_effect=fake_path):
                fake_sampler = fake_sampler_cls.return_value
                fake_sampler.samples = []
                fake_sampler.summary.return_value = {
                    "samples": 0, "sources": {"link": True, "temp": True},
                    "load_source": None,
                }
                fio_test.run_fio_test(
                    DISK, "seq_read", ["--rw=read"], diag_store={}
                )

            files = list((real_path / "reports" / "raw").glob("fio-*.json"))
            self.assertEqual(len(files), 1)
            self.assertIn("nvme0n1", files[0].name)
            self.assertIn("seq_read", files[0].name)
            self.assertEqual(files[0].read_text(encoding="utf-8"), raw_text)

    def test_raw_fio_json_not_saved_without_diag_store(self):
        raw_text = json.dumps({"jobs": [{"read": {"bw_bytes": 1000, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw_text.encode(), b"")
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "_save_raw_fio_output") as saver, \
             mock.patch.object(fio_test, "DiagnosticSampler"):
            fio_test.run_fio_test(DISK, "seq_read", ["--rw=read"])
        saver.assert_not_called()

    def test_no_diag_store_means_no_sampler(self):
        raw = json.dumps({"jobs": [{"read": {"bw_bytes": 1000, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            res = fio_test.run_fio_test(DISK, "seq_read", ["--rw=read"])

        self.assertNotIn("diag", res)
        fake_sampler_cls.assert_not_called()


class RunFioTestLogFlagsTests(unittest.TestCase):
    """В диагностическом режиме fio пишет посекундные логи нагрузки."""

    def _run(self, diag_store):
        raw = json.dumps({"jobs": [{"read": {"bw_bytes": 1000, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        captured = {}

        def fake_run(cmd, cancel_event):
            captured["cmd"] = list(cmd)
            return fake

        with mock.patch.object(fio_test, "_run_io_process", side_effect=fake_run), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            fake_sampler = fake_sampler_cls.return_value
            fake_sampler.samples = []
            fake_sampler.summary.return_value = {
                "samples": 0, "sources": {"link": True, "temp": True},
                "load_source": None,
            }
            fake_sampler.merge_fio_logs.return_value = False
            res = fio_test.run_fio_test(
                DISK, "seq_read", ["--rw=read"], diag_store=diag_store
            )
        return res, captured["cmd"], fake_sampler

    def test_log_flags_added_in_diag_mode(self):
        prefix = str(Path("reports") / "fio-nvme0n1-seq_read")
        _, cmd, sampler = self._run(diag_store={})

        self.assertIn("--write_bw_log", cmd)
        self.assertIn("--write_iops_log", cmd)
        self.assertIn("--log_avg_msec", cmd)
        self.assertIn("--log_unix_epoch", cmd)
        self.assertIn("--per_job_logs", cmd)
        self.assertIn(prefix, cmd)
        sampler.merge_fio_logs.assert_called_once_with(prefix)

    def test_no_log_flags_and_no_merge_without_diag(self):
        res, cmd, sampler = self._run(diag_store=None)

        self.assertNotIn("--write_bw_log", cmd)
        self.assertNotIn("--write_iops_log", cmd)
        sampler.merge_fio_logs.assert_not_called()
        self.assertNotIn("diag", res)

    def test_notes_for_missing_temp(self):
        raw = json.dumps({"jobs": [{"read": {"bw_bytes": 1, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            fake_sampler = fake_sampler_cls.return_value
            fake_sampler.samples = [{"ts": 1.0, "read_mbs": 1000.0,
                                     "write_mbs": 0.0, "iops": 10}]
            fake_sampler.merge_fio_logs.return_value = True
            fake_sampler.summary.return_value = {
                "samples": 1,
                "sources": {"link": True, "temp": False},
                "load_source": "fio",
            }
            res = fio_test.run_fio_test(DISK, "seq_read", ["--rw=read"], diag_store={})

        notes = res["diag"]["notes"]
        self.assertTrue(any("nvme-cli" in n for n in notes), notes)

    def test_no_notes_when_all_sources_available(self):
        raw = json.dumps({"jobs": [{"read": {"bw_bytes": 1, "iops": 1}}]})
        fake = (mock.Mock(returncode=0), raw.encode(), b"")
        with mock.patch.object(fio_test, "_run_io_process", return_value=fake), \
             mock.patch.object(fio_test, "DiagnosticSampler") as fake_sampler_cls:
            fake_sampler = fake_sampler_cls.return_value
            fake_sampler.samples = []
            fake_sampler.merge_fio_logs.return_value = True
            fake_sampler.summary.return_value = {
                "samples": 1,
                "sources": {"link": True, "temp": True},
                "load_source": "fio",
            }
            res = fio_test.run_fio_test(DISK, "seq_read", ["--rw=read"], diag_store={})

        self.assertEqual(res["diag"]["notes"], [])


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


class ParseDiskSelectionArgsTests(unittest.TestCase):
    """-a/--add и -d/--delete: парсинг, интерактив и взаимное исключение."""

    def test_add_parses_numbers(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a", "1", "2", "3"]):
            args = fio_test.parse_args()
        self.assertEqual(args.add, [1, 2, 3])
        self.assertIsNone(args.delete)

    def test_add_long_form(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "--add", "1", "3"]):
            args = fio_test.parse_args()
        self.assertEqual(args.add, [1, 3])

    def test_delete_parses_numbers(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-d", "4", "5"]):
            args = fio_test.parse_args()
        self.assertEqual(args.delete, [4, 5])
        self.assertIsNone(args.add)

    def test_add_range_expands(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a", "1-3"]):
            args = fio_test.parse_args()
        self.assertEqual(args.add, [1, 2, 3])

    def test_add_mixed_numbers_and_ranges(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a", "1-3", "5"]):
            args = fio_test.parse_args()
        self.assertEqual(args.add, [1, 2, 3, 5])

    def test_delete_range_expands(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-d", "2-4", "6"]):
            args = fio_test.parse_args()
        self.assertEqual(args.delete, [2, 3, 4, 6])

    def test_add_descending_range(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a", "3-1"]):
            args = fio_test.parse_args()
        self.assertEqual(args.add, [3, 2, 1])

    def test_invalid_token_exits(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a", "1-x"]):
            with self.assertRaises(SystemExit):
                fio_test.parse_args()

    def test_bare_add_yields_empty_list(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a"]):
            args = fio_test.parse_args()
        self.assertEqual(args.add, [])

    def test_defaults_are_none(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py"]):
            args = fio_test.parse_args()
        self.assertIsNone(args.add)
        self.assertIsNone(args.delete)

    def test_add_and_delete_are_mutually_exclusive(self):
        with mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a", "1", "-d", "2"]):
            with self.assertRaises(SystemExit):
                fio_test.parse_args()


class ExpandTokenTests(unittest.TestCase):
    def test_single_number(self):
        self.assertEqual(fio_test._expand_token("5"), [5])

    def test_ascending_range(self):
        self.assertEqual(fio_test._expand_token("1-3"), [1, 2, 3])

    def test_descending_range(self):
        self.assertEqual(fio_test._expand_token("3-1"), [3, 2, 1])

    def test_single_element_range(self):
        self.assertEqual(fio_test._expand_token("2-2"), [2])

    def test_invalid_token_raises(self):
        with self.assertRaises(ValueError):
            fio_test._expand_token("1-x")

    def test_empty_token_raises(self):
        with self.assertRaises(ValueError):
            fio_test._expand_token("")


class ParseDiskNumbersTests(unittest.TestCase):
    def test_numbers_split_on_spaces(self):
        self.assertEqual(fio_test.parse_disk_numbers(" 1  2  3 "), [1, 2, 3])

    def test_single_number(self):
        self.assertEqual(fio_test.parse_disk_numbers("5"), [5])

    def test_empty_string(self):
        self.assertEqual(fio_test.parse_disk_numbers(""), [])

    def test_whitespace_only(self):
        self.assertEqual(fio_test.parse_disk_numbers("   "), [])

    def test_range_expands(self):
        self.assertEqual(fio_test.parse_disk_numbers("1-3"), [1, 2, 3])

    def test_numbers_and_ranges_mix(self):
        self.assertEqual(fio_test.parse_disk_numbers("1-3 5"), [1, 2, 3, 5])

    def test_descending_range(self):
        self.assertEqual(fio_test.parse_disk_numbers("3-1"), [3, 2, 1])

    def test_invalid_token_raises(self):
        with self.assertRaises(ValueError):
            fio_test.parse_disk_numbers("1-x 3")


class InputDiskNumbersTests(unittest.TestCase):
    def test_reads_numbers_from_stdin(self):
        with mock.patch("builtins.input", return_value=" 1  2 "):
            self.assertEqual(fio_test._input_disk_numbers("prompt: "), [1, 2])

    def test_reads_ranges_from_stdin(self):
        with mock.patch("builtins.input", return_value="1-3 5"):
            self.assertEqual(fio_test._input_disk_numbers("prompt: "), [1, 2, 3, 5])

    def test_empty_input_returns_empty_list(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(fio_test._input_disk_numbers("prompt: "), [])

    def test_invalid_input_reprompts(self):
        answers = iter(["1-x", "3-1 5"])
        with mock.patch("builtins.input", side_effect=lambda *a: next(answers)), \
             mock.patch.object(fio_test, "console"):
            self.assertEqual(fio_test._input_disk_numbers("prompt: "), [3, 2, 1, 5])

    def test_eof_aborts(self):
        with mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(fio_test, "console"):
            with self.assertRaises(SystemExit):
                fio_test._input_disk_numbers("prompt: ")


class ApplyDiskSelectionTests(unittest.TestCase):
    def setUp(self):
        self.disks = fio_test.build_fake_disks()
        self.names = [d["name"] for d in self.disks]

    def _args(self, add=None, delete=None):
        args = mock.Mock()
        args.add = add
        args.delete = delete
        return args

    def test_add_keeps_only_selected(self):
        result = fio_test.apply_disk_selection(self.disks, self._args(add=[1, 3]))
        self.assertEqual([d["name"] for d in result], ["nvme0n1", "sda"])

    def test_delete_removes_selected(self):
        result = fio_test.apply_disk_selection(self.disks, self._args(delete=[2, 4]))
        self.assertEqual([d["name"] for d in result], ["nvme0n1", "sda", "sdc"])

    def test_add_with_all_numbers_keeps_all(self):
        result = fio_test.apply_disk_selection(self.disks, self._args(add=[1, 2, 3, 4, 5]))
        self.assertEqual([d["name"] for d in result], self.names)

    def test_delete_with_all_numbers_keeps_none(self):
        result = fio_test.apply_disk_selection(self.disks, self._args(delete=[1, 2, 3, 4, 5]))
        self.assertEqual(result, [])

    def test_no_flags_returns_unchanged(self):
        result = fio_test.apply_disk_selection(self.disks, self._args())
        self.assertEqual([d["name"] for d in result], self.names)

    def test_empty_add_yields_no_disks(self):
        result = fio_test.apply_disk_selection(self.disks, self._args(add=[]))
        self.assertEqual(result, [])

    def test_empty_delete_keeps_all_disks(self):
        result = fio_test.apply_disk_selection(self.disks, self._args(delete=[]))
        self.assertEqual([d["name"] for d in result], self.names)

    def test_out_of_range_number_exits(self):
        with mock.patch.object(fio_test, "console"):
            with self.assertRaises(SystemExit):
                fio_test.apply_disk_selection(self.disks, self._args(add=[1, 99]))

    def test_out_of_range_message_single_disk(self):
        one = [dict(self.disks[0])]
        with mock.patch.object(fio_test, "console") as fake_console:
            with self.assertRaises(SystemExit):
                fio_test.apply_disk_selection(one, self._args(add=[2]))
        text = " ".join(str(c) for c in fake_console.print.call_args.args)
        self.assertIn("доступен номер 1", text)
        self.assertNotIn("1..1", text)

    def test_out_of_range_message_multi_disk(self):
        with mock.patch.object(fio_test, "console") as fake_console:
            with self.assertRaises(SystemExit):
                fio_test.apply_disk_selection(self.disks, self._args(add=[99]))
        text = " ".join(str(c) for c in fake_console.print.call_args.args)
        self.assertIn("доступны номера 1..5", text)


class MainDiskSelectionTests(unittest.TestCase):
    """Выбор дисков через -a/-d должен доходить до run_disk_tests."""

    def _run_main(self, argv, disks):
        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv", argv), \
             mock.patch.object(fio_test, "_default_report_path",
                               return_value=Path("reports") / "t.md"), \
             mock.patch.object(fio_test, "run_disk_tests") as fake_runner:
            fio_test.main()
        return fake_runner

    def _three_disks(self):
        return [dict(DISK, name=f"nvme{i}n1", slot=f"nvme{i}") for i in range(3)]

    def test_add_selects_subset_of_disks(self):
        disks = self._three_disks()
        runner = self._run_main(["fio-test.py", "-a", "1", "3"], disks)

        self.assertEqual(runner.call_count, 2)
        tested = sorted(disk["name"] for call in runner.call_args_list
                        for _, disk, _, _ in [call.args])
        self.assertEqual(tested, ["nvme0n1", "nvme2n1"])

    def test_delete_excludes_disk(self):
        disks = self._three_disks()
        runner = self._run_main(["fio-test.py", "-d", "2"], disks)

        self.assertEqual(runner.call_count, 2)
        tested = sorted(disk["name"] for call in runner.call_args_list
                        for _, disk, _, _ in [call.args])
        self.assertEqual(tested, ["nvme0n1", "nvme2n1"])

    def test_add_range_selects_disks(self):
        disks = self._three_disks()
        runner = self._run_main(["fio-test.py", "-a", "1-3"], disks)

        self.assertEqual(runner.call_count, 3)
        tested = sorted(disk["name"] for call in runner.call_args_list
                        for _, disk, _, _ in [call.args])
        self.assertEqual(tested, ["nvme0n1", "nvme1n1", "nvme2n1"])

    def test_add_prompt_fills_numbers(self):
        disks = self._three_disks()
        with mock.patch("builtins.input", return_value="2"):
            runner = self._run_main(["fio-test.py", "-a"], disks)

        self.assertEqual(runner.call_count, 1)
        _, disk, _, _ = runner.call_args.args
        self.assertEqual(disk["name"], "nvme1n1")

    def test_add_empty_prompt_exits_without_running(self):
        disks = self._three_disks()
        runner = mock.MagicMock()
        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "console"), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-a"]), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(fio_test, "run_disk_tests", runner):
            with self.assertRaises(SystemExit) as cm:
                fio_test.main()
        self.assertEqual(cm.exception.code, 0)
        runner.assert_not_called()

    def test_delete_all_disks_exits_without_running(self):
        disks = self._three_disks()
        runner = mock.MagicMock()
        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "console"), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-d", "1", "2", "3"]), \
             mock.patch.object(fio_test, "run_disk_tests", runner):
            with self.assertRaises(SystemExit) as cm:
                fio_test.main()
        self.assertEqual(cm.exception.code, 0)
        runner.assert_not_called()


class TestModeDiskSelectionTests(unittest.TestCase):
    """В тестовом режиме используются реальные диски из сканирования; -a/-d фильтруют их."""

    def _scan(self):
        return [
            {"name": "nvme0n1", "model": "NVME A", "tran": "NVME"},
            {"name": "sda", "model": "SATA B", "tran": "SATA"},
            {"name": "sdb", "model": "SAS C", "tran": "SAS"},
        ]

    def _run(self, argv, scan):
        with mock.patch.object(fio_test, "scan_disks", return_value=([], scan)), \
             mock.patch.object(fio_test, "build_results_table", return_value=None) as fake_table, \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "SystemTuner") as fake_tuner_cls, \
             mock.patch.object(fio_test.sys, "argv", argv):
            fake_tuner = fake_tuner_cls.return_value
            fake_tuner.preview.return_value = []
            fake_tuner.get_nvme_temps.return_value = {}
            fio_test.main()
        disks, _, _ = fake_table.call_args.args
        return [d["name"] for d in disks]

    def test_add_filters_real_disks(self):
        self.assertEqual(
            self._run(["fio-test.py", "-t", "-a", "1", "3"], self._scan()),
            ["nvme0n1", "sdb"],
        )

    def test_delete_filters_real_disks(self):
        self.assertEqual(
            self._run(["fio-test.py", "-t", "-d", "2"], self._scan()),
            ["nvme0n1", "sdb"],
        )

    def test_no_selection_keeps_all_real_disks(self):
        self.assertEqual(
            self._run(["fio-test.py", "-t"], self._scan()),
            ["nvme0n1", "sda", "sdb"],
        )

    def test_empty_scan_falls_back_to_fake_disks(self):
        names = [d["name"] for d in fio_test.build_fake_disks()]
        self.assertEqual(self._run(["fio-test.py", "-t"], []), names)

    def test_fallback_prints_warning(self):
        printed = []

        def fake_print(*args, **kwargs):
            printed.append(args[0] if args else kwargs.get("text", ""))

        with mock.patch.object(fio_test, "scan_disks", return_value=([], [])), \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test, "console") as fake_console, \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-t"]):
            fake_console.print.side_effect = fake_print
            fio_test.main()
        self.assertTrue(
            any("Реальных дисков не найдено" in str(p) for p in printed)
        )

    def test_add_without_numbers_prompts_interactively(self):
        with mock.patch.object(fio_test, "scan_disks", return_value=([], self._scan())), \
             mock.patch.object(fio_test, "build_results_table", return_value=None) as fake_table, \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test, "console"), \
             mock.patch.object(fio_test, "_input_disk_numbers", return_value=[2]) as fake_input, \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py", "-t", "-a"]):
            fio_test.main()
        fake_input.assert_called_once()
        disks, _, _ = fake_table.call_args.args
        self.assertEqual([d["name"] for d in disks], ["sda"])

    def test_delete_all_exits(self):
        with mock.patch.object(fio_test, "scan_disks", return_value=([], self._scan())), \
             mock.patch.object(fio_test, "console"), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv",
                               ["fio-test.py", "-t", "-d", "1", "2", "3"]):
            with self.assertRaises(SystemExit) as cm:
                fio_test.main()
        self.assertEqual(cm.exception.code, 0)

    def test_bad_threshold_value_does_not_crash_test_mode(self):
        with mock.patch.object(fio_test, "scan_disks", return_value=([], self._scan())), \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "SystemTuner"), \
             mock.patch.object(fio_test.sys, "argv",
                               ["fio-test.py", "-t", "--threshold-nvme", "abc"]):
            fio_test.main()


class ElapsedParseTests(unittest.TestCase):

    def test_elapsed_parsed_from_job(self):
        raw = json.dumps({
            "jobs": [{
                "read": {"bw_bytes": 1000, "iops": 1},
                "elapsed": 120,
            }]
        })
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertEqual(res["elapsed_s"], 120.0)

    def test_elapsed_missing_defaults_zero(self):
        raw = json.dumps({"jobs": [{"read": {"bw_bytes": 1, "iops": 1}}]})
        res = fio_test._parse_fio_result("seq_read", raw)
        self.assertEqual(res["elapsed_s"], 0.0)


class BuildRunInfoTimingTests(unittest.TestCase):
    def test_durations_appear_in_flags(self):
        args = mock.Mock()
        args.sequential = False
        args.prefill = True
        args.logging = False
        args.no_tune = False
        args.runtime = 60
        args.add = None
        args.delete = None
        args.threshold_nvme = None
        args.threshold_sas = None
        args.threshold_sata = None
        args.output = None
        info = fio_test._build_run_info(
            args, prefill_duration=125, tests_duration=3725
        )
        flags = dict(info["flags"])
        self.assertEqual(flags["Время предзаполнения"], "2 мин 05 с")
        self.assertEqual(flags["Время тестов"], "1 ч 02 мин")

    def test_no_durations_no_extra_flags(self):
        args = mock.Mock()
        args.sequential = False
        args.prefill = False
        args.logging = False
        args.no_tune = True
        args.runtime = None
        args.add = None
        args.delete = None
        args.threshold_nvme = None
        args.threshold_sas = None
        args.threshold_sata = None
        args.output = None
        info = fio_test._build_run_info(args)
        self.assertEqual(
            dict(info["flags"]).get("Время предзаполнения"), None
        )
        self.assertEqual(dict(info["flags"]).get("Время тестов"), None)

    def test_test_mode_marks_regime_and_skips_irrelevant_flags(self):
        args = mock.Mock()
        args.sequential = True
        args.prefill = True
        args.logging = True
        args.no_tune = False
        args.runtime = 60
        args.add = [1]
        args.delete = None
        args.threshold_nvme = "seq_read=1"
        args.threshold_sas = None
        args.threshold_sata = None
        args.output = "reports/t.md"
        info = fio_test._build_run_info(args, test_mode=True)
        flags = dict(info["flags"])
        self.assertEqual(flags["Режим"], "тестовый")
        self.assertNotIn("Предварительное заполнение", flags)
        self.assertNotIn("Длительность теста", flags)
        self.assertNotIn("Пороги NVMe", flags)
        self.assertEqual(flags["Выбор дисков (--add)"], "1")
        self.assertEqual(flags["Выходной отчёт"], "reports/t.md")

    def test_normal_mode_keeps_runtime_prefill_and_thresholds(self):
        args = mock.Mock()
        args.sequential = True
        args.prefill = True
        args.logging = False
        args.no_tune = False
        args.runtime = 60
        args.add = None
        args.delete = None
        args.threshold_nvme = "seq_read=1"
        args.threshold_sas = None
        args.threshold_sata = None
        args.output = None
        info = fio_test._build_run_info(args)
        flags = dict(info["flags"])
        self.assertEqual(flags["Режим"], "последовательный")
        self.assertEqual(flags["Предварительное заполнение"], "включено")
        self.assertEqual(flags["Длительность теста"], "60 сек")
        self.assertEqual(flags["Пороги NVMe"], "seq_read=1")


class OptimizeNvmeArgsTests(unittest.TestCase):
    """Единая таблица переопределений для Gen4/Gen5 (включая seq_write)."""

    def _args(self, numjobs=None, iodepth=None, bs=None):
        out = ["--rw=read", "--bs=128k", "--iodepth=64", "--numjobs=4"]
        if numjobs is not None:
            out[3] = f"--numjobs={numjobs}"
        if iodepth is not None:
            out[2] = f"--iodepth={iodepth}"
        if bs is not None:
            out[1] = f"--bs={bs}"
        return out

    def test_gen4_seq_write_overridden_like_gen5(self):
        args = fio_test.optimize_nvme_args(
            "seq_write", self._args(), {"gen": 4, "width": 4}
        )
        self.assertIn("--numjobs=2", args)
        self.assertIn("--iodepth=16", args)

    def test_gen4_seq_read_overridden(self):
        args = fio_test.optimize_nvme_args(
            "seq_read", self._args(), {"gen": 4, "width": 4}
        )
        self.assertIn("--numjobs=2", args)
        self.assertIn("--iodepth=16", args)

    def test_gen4_rand_read_overridden(self):
        args = fio_test.optimize_nvme_args(
            "rand_read", self._args(), {"gen": 4, "width": 4}
        )
        self.assertIn("--numjobs=8", args)
        self.assertIn("--iodepth=32", args)

    def test_gen5_seq_read_overridden(self):
        args = fio_test.optimize_nvme_args(
            "seq_read", self._args(), {"gen": 5, "width": 4}
        )
        self.assertIn("--numjobs=4", args)
        self.assertIn("--iodepth=16", args)
        self.assertIn("--bs=256k", args)

    def test_gen5_rand_read_overridden(self):
        args = fio_test.optimize_nvme_args(
            "rand_read", self._args(), {"gen": 5, "width": 4}
        )
        self.assertIn("--numjobs=16", args)
        self.assertIn("--iodepth=16", args)

    def test_no_pcie_info_returns_unchanged(self):
        args = self._args()
        self.assertEqual(
            fio_test.optimize_nvme_args("seq_read", args, None), args
        )

    def test_gen_without_overrides_returns_unchanged(self):
        args = self._args()
        self.assertEqual(
            fio_test.optimize_nvme_args("seq_read", args, {"gen": 3}), args
        )


if __name__ == "__main__":
    unittest.main()
