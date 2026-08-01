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
        "name": "sda", "model": "QEMU HARDDISK", "tran": "SATA",
        "serial": "drive-scsi0", "slot": "2:0:0:0", "size": "32G",
    },
    {
        "name": "sdb", "model": "QEMU HARDDISK", "tran": "SATA",
        "serial": "drive-scsi1", "slot": "3:0:0:1", "size": "32G",
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
        "mixed": {
            "bs": "8k", "iops": 12345, "bw_mb": 96.5,
            "lat_avg": 0.81, "lat_p99": 2.25, "status": "done",
        },
    },
    {},
]

TEST_NAMES = {
    "seq_read": "1. Послед. Чтение",
    "seq_write": "2. Послед. Запись",
    "rand_read": "3. Случ. Чтение 4k",
    "rand_write": "4. Случ. Запись 4k",
    "mixed": "5. Смешанная нагрузка",
}


def render_table(renderable):
    stream = StringIO()
    console = Console(
        file=stream,
        width=160,
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
    def test_all_disks_share_one_outer_table_without_section_lines(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        lines = output.splitlines()
        self.assertEqual(sum(line.startswith("╭") for line in lines), 1)
        self.assertEqual(sum(line.startswith("╰") for line in lines), 1)
        self.assertFalse(any(line.startswith(("├", "┼", "┤")) for line in lines))
        self.assertNotIn("", lines)

    def test_column_names_repeat_for_every_disk_without_global_title(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertEqual(output.count("Профиль теста"), 2)
        self.assertEqual(output.count("Скорость (МБ/с)"), 2)
        self.assertEqual(output.count("Накопитель"), 2)
        self.assertNotIn("Результаты тестирования накопителя", output)

    def test_renderer_uses_every_configured_test_without_fixed_test_order(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertEqual(output.count("5. Смешанная нагрузка"), 2)
        self.assertIn("12,345", output)
        self.assertIn("96.5", output)

    def test_disk_details_and_test_results_are_direct_cells(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertIn("/dev/sda", output)
        self.assertIn("SN: drive-scsi0", output)
        self.assertIn("/dev/sdb", output)
        self.assertIn("SN: drive-scsi1", output)
        self.assertEqual(output.count("╭"), 1)

    def test_entrypoint_exposes_flat_renderer(self):
        entrypoint = load_entrypoint()

        output = render_table(
            entrypoint.build_results_table(DISKS, RESULTS, TEST_NAMES)
        )

        self.assertEqual(output.count("╭"), 1)
        self.assertEqual(output.count("Профиль теста"), 2)

    def test_only_status_values_receive_color_styles(self):
        done = format_status("done")
        undone = format_status("undone")

        self.assertEqual(done.plain, "done")
        self.assertEqual(str(done.style), "bold green")
        self.assertEqual(undone.plain, "undone")
        self.assertEqual(str(undone.style), "bold red")


if __name__ == "__main__":
    unittest.main()
