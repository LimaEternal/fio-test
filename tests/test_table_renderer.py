from io import StringIO
import importlib.util
from pathlib import Path
import sys
import unittest

from rich.console import Console


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.table_renderer import build_results_table, format_status


DISKS = [
    {
        "name": "sda",
        "model": "QEMU HARDDISK",
        "tran": "SATA",
        "serial": "drive-scsi0",
        "slot": "2:0:0:0",
        "size": "32G",
    },
    {
        "name": "sdb",
        "model": "QEMU HARDDISK",
        "tran": "SATA",
        "serial": "drive-scsi1",
        "slot": "3:0:0:1",
        "size": "32G",
    },
]

RESULTS = [
    {
        "seq_read": {
            "bs": "64k", "iops": 73390, "bw_mb": 4586.9,
            "lat_avg": 0.44, "lat_p99": 0.0, "status": "done",
        },
        "seq_write": {
            "bs": "64k", "iops": 8169, "bw_mb": 510.6,
            "lat_avg": 3.8, "lat_p99": 0.0, "status": "done",
        },
        "rand_read": {
            "bs": "4k", "iops": 170566, "bw_mb": 666.3,
            "lat_avg": 0.19, "lat_p99": 0.0, "status": "done",
        },
        "rand_write": {
            "bs": "4k", "iops": 20322, "bw_mb": 79.4,
            "lat_avg": 1.53, "lat_p99": 0.0, "status": "undone",
        },
    },
    {},
]

TEST_NAMES = {
    "seq_read": "1. Послед. Чтение",
    "seq_write": "2. Послед. Запись",
    "rand_read": "3. Случ. Чтение 4k",
    "rand_write": "4. Случ. Запись 4k",
}


def render_table(renderable):
    stream = StringIO()
    console = Console(
        file=stream,
        width=150,
        color_system=None,
        highlight=False,
        legacy_windows=False,
    )
    console.print(renderable)
    return stream.getvalue()


def load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "fio_test_entrypoint", PROJECT_ROOT / "fio-test.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TableRendererTests(unittest.TestCase):
    def test_entrypoint_exposes_per_disk_tables(self):
        entrypoint = load_entrypoint()

        output = render_table(
            entrypoint.build_results_table(DISKS, RESULTS, TEST_NAMES, gap=0)
        )

        self.assertEqual(
            output.count("Результаты тестирования накопителя (FIO)"), 2
        )

    def test_each_disk_has_own_header_and_blocks_touch(self):
        output = render_table(
            build_results_table(DISKS, RESULTS, TEST_NAMES, gap=0)
        )

        self.assertEqual(
            output.count("Результаты тестирования накопителя (FIO)"), 2
        )
        self.assertIn("/dev/sda", output)
        self.assertIn("/dev/sdb", output)

        lines = output.splitlines()
        first_bottom = next(
            index for index, line in enumerate(lines) if line.startswith("╰")
        )
        self.assertTrue(lines[first_bottom + 1].startswith("╭"))

    def test_positive_gap_is_centralized_rendering_option(self):
        output = render_table(
            build_results_table(DISKS, RESULTS, TEST_NAMES, gap=1)
        )

        lines = output.splitlines()
        first_bottom = next(
            index for index, line in enumerate(lines) if line.startswith("╰")
        )
        self.assertEqual(lines[first_bottom + 1], "")
        self.assertTrue(lines[first_bottom + 2].startswith("╭"))

    def test_all_disk_blocks_use_the_full_available_width(self):
        output = render_table(
            build_results_table(DISKS, RESULTS, TEST_NAMES, gap=0)
        )

        top_borders = [line for line in output.splitlines() if line.startswith("╭")]
        self.assertEqual(len(top_borders), 2)
        self.assertEqual({len(line) for line in top_borders}, {150})

    def test_only_status_values_receive_explicit_styles(self):
        done = format_status("done")
        undone = format_status("undone")

        self.assertEqual(done.plain, "done")
        self.assertEqual(str(done.style), "bold green")
        self.assertEqual(undone.plain, "undone")
        self.assertEqual(str(undone.style), "bold red")


if __name__ == "__main__":
    unittest.main()
