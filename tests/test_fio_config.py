import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.fio_config import FioConfigError, parse_fio_jobfile  # noqa: E402


def _load_fio_test():
    spec = importlib.util.spec_from_file_location(
        "fio_test", PROJECT_ROOT / "fio-test.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParseFioJobfileTests(unittest.TestCase):
    def _parse(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.fio"
            path.write_text(content, encoding="utf-8")
            return parse_fio_jobfile(path)

    def test_parses_sections_in_order(self):
        result = self._parse(
            "[seq_read]\n"
            "rw=read\n"
            "bs=128k\n"
            "\n"
            "[seq_write]\n"
            "rw=write\n"
            "bs=128k\n"
        )
        self.assertEqual(list(result.keys()), ["seq_read", "seq_write"])
        self.assertEqual(result["seq_read"], ["--rw=read", "--bs=128k"])
        self.assertEqual(result["seq_write"], ["--rw=write", "--bs=128k"])

    def test_global_merged_into_each_section(self):
        result = self._parse(
            "[global]\n"
            "ioengine=libaio\n"
            "direct=1\n"
            "[seq_read]\n"
            "rw=read\n"
        )
        self.assertEqual(
            result["seq_read"],
            ["--ioengine=libaio", "--direct=1", "--rw=read"],
        )
        self.assertNotIn("global", result)

    def test_section_option_overrides_global(self):
        result = self._parse(
            "[global]\n"
            "runtime=30\n"
            "[seq_write]\n"
            "rw=write\n"
            "runtime=60\n"
        )
        self.assertEqual(result["seq_write"], ["--runtime=30", "--rw=write", "--runtime=60"])

    def test_global_after_sections_still_applies(self):
        result = self._parse(
            "[seq_read]\n"
            "rw=read\n"
            "[global]\n"
            "ioengine=libaio\n"
        )
        self.assertEqual(result["seq_read"], ["--ioengine=libaio", "--rw=read"])

    def test_comments_and_blank_lines_ignored(self):
        result = self._parse(
            "# header comment\n"
            "; another comment\n"
            "\n"
            "   # indented comment\n"
            "[seq_read]\n"
            "rw=read\n"
        )
        self.assertEqual(result["seq_read"], ["--rw=read"])

    def test_option_before_any_section_raises(self):
        with self.assertRaises(FioConfigError):
            self._parse("rw=read\n")

    def test_bad_line_raises(self):
        with self.assertRaises(FioConfigError):
            self._parse("[seq_read]\nthis is not an option\n")

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FioConfigError):
                parse_fio_jobfile(Path(tmp) / "nope.fio")

    def test_only_global_section_raises(self):
        with self.assertRaises(FioConfigError):
            self._parse("[global]\nioengine=libaio\n")


class ProjectFioConfigsTests(unittest.TestCase):
    """Реальные конфиги проекта должны парситься и проходить валидацию."""

    def test_all_interface_configs_parse(self):
        fio_test = _load_fio_test()
        self.assertEqual(
            list(fio_test.INTERFACE_CONFIGS.keys()), ["nvme", "sas", "sata"]
        )
        for iface, tests in fio_test.INTERFACE_CONFIGS.items():
            self.assertEqual(
                list(tests.keys()),
                ["seq_read", "seq_write", "rand_read", "rand_write"],
            )
            for args in tests.values():
                self.assertTrue(any(a.startswith("--ioengine=") for a in args))
                self.assertIn("--direct=1", args)
                self.assertIn("--output-format=json", args)
                self.assertFalse(any(a.startswith("--fsync=") for a in args))


if __name__ == "__main__":
    unittest.main()
