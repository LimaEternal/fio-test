"""Матрица флагов: исчерпывающий перебор комбинаций режимов.

Цель — убедиться, что main() не падает и корректно завершается при любом
сочетании флагов (включая неочевидные и «молча игнорируемые»), а также что
некорректные значения отсекаются на этапе parse_args.

Всё выполняется с замоканными scan_disks / fio / префиллом / тюнером, поэтому
реальные диски и утилиты не трогаются.
"""

import itertools
import sys
import unittest
from contextlib import contextmanager
from importlib import util
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

_spec = util.spec_from_file_location("fio_test", PROJECT_ROOT / "fio-test.py")
fio_test = util.module_from_spec(_spec)
_spec.loader.exec_module(fio_test)


DISK = {
    "name": "nvme0n1", "path": "/dev/nvme0n1", "model": "KIOXIA KCMY1VUG3T20",
    "serial": "SN", "tran": "NVME", "size": "3.2T", "phy_sec": 4096,
    "slot": "nvme0", "root_partition": None,
}


@contextmanager
def cli(argv):
    """Подменяет всё, что main() трогает снаружи, и выставляет sys.argv."""
    disks = [dict(DISK)]
    tuner = mock.MagicMock()
    tuner.return_value.get_nvme_temps.return_value = {}
    tuner.return_value.governor_failed = False
    patches = [
        mock.patch.object(fio_test, "scan_disks", return_value=([], [], disks)),
        mock.patch.object(fio_test, "generate_report", return_value="rep.md"),
        mock.patch.object(fio_test, "build_results_table", return_value=None),
        mock.patch.object(
            fio_test.subprocess, "run", return_value=mock.Mock(returncode=0)
        ),
        mock.patch.object(fio_test, "SystemTuner", tuner),
        mock.patch.object(fio_test.sys, "argv", argv),
        mock.patch.object(
            fio_test, "_default_report_path", return_value=Path("reports") / "t.md"
        ),
        mock.patch.object(fio_test, "run_disk_tests"),
        mock.patch.object(fio_test, "prefill_disks", return_value=0.0),
        mock.patch.object(
            fio_test, "collect_static_info",
            return_value={"numa_node": None, "cpu_affinity": None},
        ),
        mock.patch.object(fio_test, "input", return_value="n"),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


def _run(argv):
    with cli(argv):
        return fio_test.main()


class FlagMatrixTests(unittest.TestCase):
    """Перебор всех булевых флагов {-s,-c,-f,-l,-t}."""

    FLAGS = ["-s", "-c", "-f", "-l", "-t"]

    def test_boolean_matrix(self):
        for combo in itertools.product([False, True], repeat=len(self.FLAGS)):
            argv = ["fio-test.py"] + [
                f for f, on in zip(self.FLAGS, combo) if on
            ]
            with self.subTest(argv=" ".join(argv)):
                code = _run(argv)
                self.assertIsInstance(code, int)
                self.assertIn(code, (0, 1, 2))

    def test_value_flag_combos(self):
        combos = [
            ["-r", "30"],
            ["-b", "200"],
            ["-b", "0"],
            ["--target-iops", "1000"],
            ["-a", "1"],
            ["-d", "1"],
            ["-o", "custom.md"],
            ["-s", "-r", "30", "-b", "200"],
            ["-l", "-o", "custom.md", "-a", "1"],
            ["-f", "-r", "30"],
            ["-l", "--target-iops", "2000"],
        ]
        for argv in combos:
            with self.subTest(argv=" ".join(argv)):
                code = _run(["fio-test.py"] + argv)
                self.assertIsInstance(code, int)
                self.assertIn(code, (0, 1, 2))

    def test_t_ignores_other_flags(self):
        # -t молча игнорирует -s/-f/-r/-b/-c (по решению).
        argv = [
            "-t", "-s", "-f", "-r", "60", "-b", "200", "-c",
        ]
        code = _run(["fio-test.py"] + argv)
        self.assertEqual(code, 0)


class FlagNegativeTests(unittest.TestCase):
    """Некорректные значения/комбинации должны отсекаться parse_args."""

    BAD = [
        ["-r", "0"],
        ["-r", "-1"],
        ["-b", "-1"],
        ["--target-iops", "0"],
        ["-a", "1", "-d", "2"],
        ["-o", str(PROJECT_ROOT)],  # существующий каталог, а не файл
    ]

    def test_invalid_args_exit(self):
        for argv in self.BAD:
            with self.subTest(argv=" ".join(argv)):
                with self.assertRaises(SystemExit):
                    _run(["fio-test.py"] + argv)


if __name__ == "__main__":
    unittest.main()
