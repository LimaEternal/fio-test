from io import StringIO
import importlib.util
from pathlib import Path
import re
import sys
import unittest

from rich.console import Console


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.table_renderer import TITLE, build_results_table, format_status


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


def render_table(renderable, width=160):
    stream = StringIO()
    console = Console(
        file=stream,
        width=width,
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
    def test_single_outer_table_with_heavy_head_and_section_between_disks(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        lines = output.splitlines()
        self.assertEqual(sum(line.startswith("┏") for line in lines), 1)
        self.assertEqual(sum(line.startswith("└") for line in lines), 1)
        self.assertEqual(sum(line.startswith("┡") for line in lines), 1)
        sections = [line for line in lines if line.startswith("├")]
        self.assertEqual(len(sections), 1)
        self.assertIn("┼", sections[0])

    def test_content_lines_have_three_columns(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        content = [
            line for line in output.splitlines()
            if line.startswith("│") and not line.startswith("├")
        ]
        self.assertTrue(content)
        for line in content:
            self.assertEqual(line.count("│"), 4)

    def test_single_header_row_with_number_disk_and_global_title(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        header_rows = [
            line for line in output.splitlines()
            if "№" in line and "Накопитель" in line and TITLE in line
        ]
        self.assertEqual(len(header_rows), 1)
        self.assertEqual(output.count("№"), 1)
        self.assertEqual(output.count(TITLE), 1)

    def test_number_in_own_column_without_disk_name_prefix(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertNotIn("1. /dev/sda", output)
        self.assertIn("/dev/sda", output)
        self.assertTrue(re.search(r"│\s+1\s+│", output))
        self.assertTrue(re.search(r"│\s+2\s+│", output))

    def test_sub_table_headers_repeat_for_every_disk(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertEqual(output.count("Профиль теста"), 2)
        self.assertEqual(output.count("Скорость (МБ/с)"), 2)
        self.assertEqual(output.count("Lat p99 (мс)"), 2)

    def test_long_test_names_fold_to_several_lines(self):
        output = render_table(
            build_results_table(DISKS, RESULTS, TEST_NAMES), width=110
        )

        lines = output.splitlines()
        self.assertTrue(any(
            "Послед." in line and "Чтение" not in line for line in lines
        ))
        self.assertTrue(any(
            "Чтение" in line and "Послед." not in line for line in lines
        ))

    def test_renderer_uses_every_configured_test_without_fixed_test_order(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertEqual(output.count("5. Смешанная нагрузка"), 2)
        self.assertIn("12,345", output)
        self.assertIn("96.5", output)

    def test_passport_details_are_in_disk_column(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        for needle in (
            "/dev/sda", "SN: drive-scsi0", "Slot: 2:0:0:0", "Размер: 32G",
            "/dev/sdb", "SN: drive-scsi1",
        ):
            self.assertIn(needle, output)

    def test_statuses_appear_once_per_test_per_disk(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        self.assertEqual(len(re.findall(r"\bdone\b", output)), 4)
        self.assertEqual(len(re.findall(r"\bundone\b", output)), 6)

    def test_entrypoint_exposes_flat_renderer(self):
        entrypoint = load_entrypoint()

        output = render_table(
            entrypoint.build_results_table(DISKS, RESULTS, TEST_NAMES)
        )

        self.assertEqual(output.count("┏"), 1)
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
