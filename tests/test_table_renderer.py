from io import StringIO
import importlib.util
from pathlib import Path
import re
import sys
import unittest

from rich.console import Console


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.table_renderer import (
    BASE_RESULT_HEADERS,
    TITLE,
    _block_size_line,
    _disk_details,
    build_results_table,
    format_status,
)


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
            "lat_avg": 0.44, "status": "PASS",
        },
        "seq_write": {
            "bs": "64k", "iops": 8169, "bw_mb": 510.6,
            "lat_avg": 3.8, "status": "PASS",
        },
        "rand_read": {
            "bs": "4k", "iops": 170566, "bw_mb": 666.3,
            "lat_avg": 0.19, "status": "PASS",
        },
        "rand_write": {
            "bs": "4k", "iops": 20322, "bw_mb": 79.4,
            "lat_avg": 1.53, "status": "FAIL",
        },
        "mixed": {
            "bs": "8k", "iops": 12345, "bw_mb": 96.5,
            "lat_avg": 0.81, "status": "PASS",
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
    def test_single_outer_table_with_simple_box_and_section_between_disks(self):
        output = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))

        lines = output.splitlines()
        self.assertEqual(sum(line.startswith("╭") for line in lines), 1)
        self.assertEqual(sum(line.startswith("╰") for line in lines), 1)
        self.assertFalse(any(line.startswith("┏") for line in lines))
        separators = [line for line in lines if line.startswith("├")]
        self.assertEqual(len(separators), 2)  # под шапкой + между дисками
        for separator in separators:
            self.assertIn("┼", separator)

    def test_numeric_result_columns_are_centered_under_headers(self):
        columns = {header: justify for header, justify, _ in BASE_RESULT_HEADERS}

        for header in ("IOPS", "Скорость (МБ/с)", "Lat Avg (мс)"):
            self.assertEqual(columns[header], "center")

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
        self.assertEqual(output.count("Lat Avg (мс)"), 2)

    def test_long_test_names_fold_to_several_lines(self):
        output = render_table(
            build_results_table(DISKS, RESULTS, TEST_NAMES), width=100
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

        self.assertEqual(len(re.findall(r"\bPASS\b", output)), 4)
        self.assertEqual(len(re.findall(r"\bFAIL\b", output)), 6)

    def test_entrypoint_exposes_flat_renderer(self):
        entrypoint = load_entrypoint()

        output = render_table(
            entrypoint.build_results_table(DISKS, RESULTS, TEST_NAMES)
        )

        self.assertEqual(output.count("╭"), 1)
        self.assertEqual(output.count("Профиль теста"), 2)

    def test_only_status_values_receive_color_styles(self):
        passed = format_status("PASS")
        failed = format_status("FAIL")

        self.assertEqual(passed.plain, "PASS")
        self.assertEqual(str(passed.style), "bold green")
        self.assertEqual(failed.plain, "FAIL")
        self.assertEqual(str(failed.style), "bold red")

    def test_tmax_column_always_present_lat_p99_only_in_logging(self):
        plain = render_table(build_results_table(DISKS, RESULTS, TEST_NAMES))
        self.assertIn("Tmax (°C)", plain)
        self.assertNotIn("Lat p99 (мс)", plain)

        logging = render_table(
            build_results_table(DISKS, RESULTS, TEST_NAMES, show_lat_p99=True)
        )
        self.assertIn("Tmax (°C)", logging)
        self.assertIn("Lat p99 (мс)", logging)

    def test_tmax_value_from_diag(self):
        results = [
            {"seq_read": {
                "bs": "64k", "iops": 73390, "bw_mb": 4586.9,
                "lat_avg": 0.44, "status": "PASS",
                "diag": {"temp_max_c": 61.5},
            }},
            {},
        ]
        output = render_table(build_results_table(DISKS, results, TEST_NAMES))
        self.assertIn("61.5", output)

    def test_fake_test_mode_data_renders_without_errors(self):
        entrypoint = load_entrypoint()
        disks = entrypoint.build_fake_disks()
        results = entrypoint.build_fake_results(disks)

        output = render_table(
            build_results_table(disks, results, entrypoint.TEST_NAMES)
        )

        self.assertEqual(len(disks), 5)
        for name in ("nvme0n1", "nvme1n1", "sda", "sdb", "sdc"):
            self.assertIn(f"/dev/{name}", output)
        self.assertGreaterEqual(output.count("test"), 100)
        self.assertNotIn("Lat p99 (мс)", output)
        self.assertIn("Tmax (°C)", output)


class DiskDetailsPcieTests(unittest.TestCase):
    """Поколение PCIe, пропускная способность и downgrade в колонке 'Накопитель'."""

    def _nvme_downgrade_disk(self):
        return {
            "name": "nvme1n1", "model": "SAMSUNG", "tran": "NVME",
            "serial": "SN", "slot": "nvme1", "size": "1.7T",
            "profile": {
                "interface": "nvme",
                "link": {
                    "gen": 4, "width": 4, "speed_gts": 16.0,
                    "max_gen": 5, "max_width": 4, "max_speed_gts": 32.0,
                    "source": "sysfs",
                },
            },
        }

    def test_nvme_downgrade_renders_gen_bandwidth_and_warning(self):
        lines = _disk_details(self._nvme_downgrade_disk())
        text = "\n".join(lines)
        self.assertIn("PCIe 4 x4", text)
        self.assertIn("Пропускная: 7877 МБ/с", text)
        self.assertIn("downgrade: есть (накопитель Gen5, порт Gen4)", text)

    def test_nvme_no_downgrade_no_warning(self):
        disk = {
            "name": "nvme0n1", "model": "SAMSUNG", "tran": "NVME",
            "serial": "SN", "slot": "nvme0", "size": "1.7T",
            "profile": {
                "interface": "nvme",
                "logical_block_size": 512, "physical_block_size": 4096,
                "link": {"gen": 5, "width": 4, "speed_gts": 32.0,
                         "max_gen": 5, "source": "sysfs"},
            },
        }
        text = "\n".join(_disk_details(disk))
        self.assertIn("PCIe 5 x4", text)
        self.assertIn("Блок (лог/физ): 512B / 4096B", text)
        self.assertIn("Пропускная: 15754 МБ/с", text)
        self.assertIn("downgrade: нет", text)
        self.assertNotIn("⚠", text)
        self.assertIn("MaxPayload: N/A", text)

    def test_nvme_downgrade_unknown_max(self):
        disk = {
            "name": "nvme0n1", "model": "SAMSUNG", "tran": "NVME",
            "serial": "SN", "slot": "nvme0", "size": "1.7T",
            "profile": {
                "interface": "nvme",
                "link": {"gen": 4, "width": 4, "speed_gts": 16.0,
                         "source": "sysfs"},
            },
        }
        text = "\n".join(_disk_details(disk))
        self.assertIn("downgrade: ?", text)

    def test_nvme_maxpayload_limited(self):
        disk = {
            "name": "nvme1n1", "model": "SAMSUNG", "tran": "NVME",
            "serial": "SN", "slot": "nvme1", "size": "1.7T",
            "profile": {
                "interface": "nvme",
                "link": {"gen": 4, "width": 4, "speed_gts": 16.0,
                         "max_gen": 5, "max_payload": {"device": 128, "port": 512},
                         "source": "sysfs"},
            },
        }
        text = "\n".join(_disk_details(disk))
        self.assertIn("MaxPayload: устройство 128B", text)
        self.assertIn(
            "⚠ MaxPayload: устройство 128B < порт 512B (лимитит пропускную способность)",
            text,
        )
        self.assertIn("downgrade: есть (накопитель Gen5, порт Gen4)", text)

    def test_nvme_maxpayload_ok(self):
        disk = {
            "name": "nvme0n1", "model": "SAMSUNG", "tran": "NVME",
            "serial": "SN", "slot": "nvme0", "size": "1.7T",
            "profile": {
                "interface": "nvme",
                "link": {"gen": 5, "width": 4, "speed_gts": 32.0,
                         "max_gen": 5, "max_payload": {"device": 256, "port": 512},
                         "source": "sysfs"},
            },
        }
        text = "\n".join(_disk_details(disk))
        self.assertIn("MaxPayload: 256B", text)
        self.assertNotIn("⚠", text)

    def test_sas_bandwidth_and_downgrade(self):
        disk = {
            "name": "sda", "model": "SEAGATE", "tran": "SAS",
            "serial": "SN", "slot": "0:0:0:0", "size": "1.8T",
            "profile": {
                "interface": "sas",
                "logical_block_size": 512, "physical_block_size": 4096,
                "link": {"negotiated_gbps": 6.0, "maximum_gbps": 12.0,
                         "source": "sas_phy"},
            },
        }
        text = "\n".join(_disk_details(disk))
        self.assertIn("SAS 6 Gbps", text)
        self.assertIn("Блок (лог/физ): 512B / 4096B", text)
        self.assertIn("Пропускная: 600 МБ/с", text)
        self.assertIn("MaxPayload: HBA Only", text)
        self.assertIn("downgrade: есть (порт 6 Gbps, накопитель 12 Gbps)", text)

    def test_block_size_line(self):
        # Оба размера есть -> форматируем
        self.assertEqual(
            _block_size_line({"logical_block_size": 512, "physical_block_size": 4096}),
            "Блок (лог/физ): 512B / 4096B",
        )
        # Чего-то нет -> строки нет
        self.assertIsNone(_block_size_line({"logical_block_size": 512}))
        self.assertIsNone(_block_size_line({}))
        # 4K-native диск
        self.assertEqual(
            _block_size_line({"logical_block_size": 4096, "physical_block_size": 4096}),
            "Блок (лог/физ): 4096B / 4096B",
        )

    def test_no_profile_keeps_legacy_lines(self):
        disk = {"name": "sda", "model": "QEMU", "tran": "SATA",
                "serial": "x", "slot": "0:0", "size": "32G"}
        lines = _disk_details(disk)
        self.assertEqual(lines, (
            "/dev/sda", "QEMU", "SATA", "SN: x", "Slot: 0:0", "Размер: 32G",
        ))


if __name__ == "__main__":
    unittest.main()
