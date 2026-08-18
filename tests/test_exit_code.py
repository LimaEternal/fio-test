import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.exit_code import (
    count_statuses,
    decide_exit_code,
    extract_statuses,
    sys_exit,
)


def _mk_disk(*statuses):
    """Имитирует results[disk_idx] проекта: {test_id: {"status": ...}}."""
    test_ids = ["seq_read", "seq_write", "rand_read", "rand_write"]
    disk = {"_thresholds": {}, "_wall_s": 1.0}
    for tid, st in zip(test_ids, statuses):
        disk[tid] = {"status": st, "iops": 1, "bw_mb": 1}
    return disk


class ExtractStatusesTests(unittest.TestCase):
    def test_ignores_private_keys(self):
        disk = _mk_disk("PASS", "FAIL", "PASS", "FAIL")
        self.assertEqual(extract_statuses([disk]), ["PASS", "FAIL", "PASS", "FAIL"])

    def test_multiple_disks(self):
        results = [
            _mk_disk("PASS", "PASS", "PASS", "PASS"),
            _mk_disk("FAIL", "FAIL", "FAIL", "FAIL"),
        ]
        self.assertEqual(len(extract_statuses(results)), 8)

    def test_flat_list_dicts(self):
        self.assertEqual(
            extract_statuses([{"status": "pass"}, {"status": "fail"}]),
            ["PASS", "FAIL"],
        )


class CountStatusesTests(unittest.TestCase):
    def test_mixed(self):
        self.assertEqual(count_statuses([_mk_disk("PASS", "FAIL", "PASS", "FAIL")]), (2, 4))

    def test_empty(self):
        self.assertEqual(count_statuses([]), (0, 0))


class DecideExitCodeTests(unittest.TestCase):
    def test_all_pass(self):
        self.assertEqual(decide_exit_code([_mk_disk("PASS", "PASS", "PASS", "PASS")]), 0)

    def test_all_fail(self):
        self.assertEqual(decide_exit_code([_mk_disk("FAIL", "FAIL", "FAIL", "FAIL")]), 1)

    def test_partial_fail(self):
        self.assertEqual(decide_exit_code([_mk_disk("PASS", "FAIL", "PASS", "PASS")]), 2)

    def test_one_disk_fail_rest_pass(self):
        results = [
            _mk_disk("PASS", "PASS", "PASS", "PASS"),
            _mk_disk("FAIL", "FAIL", "FAIL", "FAIL"),
        ]
        self.assertEqual(decide_exit_code(results), 2)

    def test_single_pass(self):
        self.assertEqual(decide_exit_code([_mk_disk("PASS")]), 0)

    def test_single_fail(self):
        self.assertEqual(decide_exit_code([_mk_disk("FAIL")]), 1)

    def test_empty(self):
        self.assertEqual(decide_exit_code([]), 0)

    def test_case_insensitive(self):
        self.assertEqual(
            decide_exit_code([{"status": "pass"}, {"status": "fail"}]), 2
        )


class SysExitTests(unittest.TestCase):
    def test_exit_0(self):
        with self.assertRaises(SystemExit) as ctx:
            sys_exit([_mk_disk("PASS", "PASS")])
        self.assertEqual(ctx.exception.code, 0)

    def test_exit_1(self):
        with self.assertRaises(SystemExit) as ctx:
            sys_exit([_mk_disk("FAIL", "FAIL")])
        self.assertEqual(ctx.exception.code, 1)

    def test_exit_2(self):
        with self.assertRaises(SystemExit) as ctx:
            sys_exit([_mk_disk("PASS", "FAIL", "PASS")])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
