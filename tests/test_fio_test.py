import sys
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

        def fake_run(disk, test_id, fio_args, cancel_event=None):
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

        def fake_run(disk, test_id, fio_args, cancel_event=None):
            call_log.append(test_id)
            if test_id == "seq_read":
                return {"error": "fio не найден"}
            return {"iops": 1, "bw_mb": 1, "lat_avg": 0.1, "lat_p99": 0.2}

        with mock.patch.object(fio_test, "run_fio_test", side_effect=fake_run):
            fio_test.run_disk_tests(0, DISK, plan, results)

        self.assertEqual(call_log, ["seq_read", "seq_write"])
        self.assertIn("error", results[0]["seq_read"])
        self.assertIn("seq_write", results[0])


class MainParallelModeTests(unittest.TestCase):
    """Параллельный режим должен отправлять в пул по одной задаче на диск."""

    def test_parallel_mode_submits_one_task_per_disk(self):
        disks = [dict(DISK, name="nvme0n1", slot="nvme0"),
                 dict(DISK, name="nvme1n1", slot="nvme1")]

        with mock.patch.object(fio_test, "scan_disks", return_value=([], disks)), \
             mock.patch.object(fio_test, "generate_report", return_value="rep.md"), \
             mock.patch.object(fio_test, "build_results_table", return_value=None), \
             mock.patch.object(fio_test.sys, "argv", ["fio-test.py"]), \
             mock.patch.object(fio_test, "run_disk_tests") as fake_runner:
            fio_test.main()

        self.assertEqual(fake_runner.call_count, 2)
        for call in fake_runner.call_args_list:
            args, kwargs = call
            _, disk, plan, _ = args
            self.assertEqual([t for t, _ in plan], NVME_TEST_IDS)
            self.assertEqual(kwargs.get("cancel_event") is not None, True)


if __name__ == "__main__":
    unittest.main()
