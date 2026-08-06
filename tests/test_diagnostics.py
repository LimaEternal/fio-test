import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.diagnostics import (
    DiagnosticSampler,
    collect_static_info,
    parse_fio_logs,
)


DISK = {
    "name": "nvme1n1", "path": "/dev/nvme1n1", "tran": "NVME",
}


def _patch_paths(tmp):
    """Перенаправляет /sys/class/nvme и /sys/class/block на временный каталог."""
    root = str(tmp)

    def _mapped(path):
        p = str(path)
        if p.startswith("/sys/class/nvme") or p.startswith("/sys/class/block"):
            return Path(root) / "sys" / p[len("/sys"):].lstrip("/")
        return Path(p)

    return mock.patch("utils.diagnostics.Path", side_effect=_mapped)


def _smart_log(text, returncode=0):
    """Мок вывода `nvme smart-log`: обычный или с ошибкой."""
    return mock.patch(
        "utils.diagnostics.subprocess.run",
        return_value=mock.Mock(returncode=returncode, stdout=text.encode()),
    )


class DiagnosticSamplerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Линк и NUMA-нода
        dev = self.tmp / "sys" / "class" / "nvme" / "nvme1" / "device"
        dev.mkdir(parents=True)
        (dev / "current_link_speed").write_text("32.0 GT/s PCIe", encoding="utf-8")
        (dev / "current_link_width").write_text("x4", encoding="utf-8")
        (dev / "numa_node").write_text("1", encoding="utf-8")

        self.link_dir = dev

    def test_link_and_temperature_are_read(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=self.link_dir), \
             _smart_log("temperature: 41 C (314 Kelvin)"):
            sampler = DiagnosticSampler(DISK)
            self.assertEqual(sampler._read_link(), (32.0, 4))
            self.assertEqual(sampler._read_temp(), 41.0)

    def test_temp_read_from_namespace_fallback(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[2])
            if cmd[2] == "/dev/nvme1":
                return mock.Mock(returncode=1, stdout=b"")
            return mock.Mock(returncode=0, stdout=b"temperature: 38 C")

        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None), \
             mock.patch("utils.diagnostics.subprocess.run", side_effect=fake_run):
            sampler = DiagnosticSampler(DISK)
            temp = sampler._read_temp()

        self.assertEqual(temp, 38.0)
        self.assertEqual(calls, ["/dev/nvme1", "/dev/nvme1n1"])

    def test_temp_parses_degree_sign_format(self):
        """nvme-cli 2.3 пишет `28°C` без пробела — градус должен распознаваться."""
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None), \
             _smart_log("temperature                             : 28°C (301 Kelvin)\n"
                        "Temperature Sensor 1           : 28°C (301 Kelvin)"):
            sampler = DiagnosticSampler(DISK)
            self.assertEqual(sampler._read_temp(), 28.0)

    def test_temp_cached_after_first_read(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None), \
             _smart_log("temperature: 35 C") as smart:
            sampler = DiagnosticSampler(DISK)
            self.assertEqual(sampler._read_temp(), 35.0)
            self.assertEqual(sampler._read_temp(), 35.0)
        self.assertEqual(smart.call_count, 1)

    def test_temp_none_when_smart_log_fails(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None), \
             _smart_log("nvme: failed", returncode=1):
            sampler = DiagnosticSampler(DISK)
            self.assertIsNone(sampler._read_temp())

    def test_temp_none_when_smart_log_unparseable(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None), \
             _smart_log("temperature: n/a C"):
            sampler = DiagnosticSampler(DISK)
            self.assertIsNone(sampler._read_temp())

    def test_sample_once_reads_link_and_temp_only(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=self.link_dir), \
             _smart_log("temperature: 41 C"):
            sampler = DiagnosticSampler(DISK)
            sampler._sample_once()
            sample = sampler.samples[0]
            summary = sampler.summary()

        self.assertEqual(sample["gts"], 32.0)
        self.assertEqual(sample["width"], 4)
        self.assertEqual(sample["temp"], 41.0)
        # Нагрузка не сэмплируется: её даёт сам fio (логи вливаются после теста).
        self.assertIsNone(sample["read_mbs"])
        self.assertIsNone(sample["write_mbs"])
        self.assertIsNone(sample["iops"])
        self.assertEqual(summary["link_gts_min"], 32.0)
        self.assertEqual(summary["link_width_min"], 4)
        self.assertEqual(summary["temp_max_c"], 41.0)
        self.assertIsNone(summary["read_mbs_avg"])
        self.assertEqual(summary["sources"]["link"], True)
        self.assertEqual(summary["sources"]["temp"], True)
        self.assertIsNone(summary["load_source"])

    def test_source_status_and_none_values_when_sources_missing(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None), \
             _smart_log("temperature: n/a C", returncode=1):
            sampler = DiagnosticSampler(DISK)
            sampler._sample_once()
            sample = sampler.samples[0]

        self.assertEqual(sample["gts"], None)
        self.assertEqual(sample["temp"], None)
        self.assertIsNone(sample["read_mbs"])
        self.assertIsNone(sample["write_mbs"])
        self.assertEqual(sampler.source_status, {"link": False, "temp": False})

        summary = sampler.summary()
        self.assertEqual(summary["sources"]["link"], False)
        self.assertEqual(summary["sources"]["temp"], False)
        self.assertIsNone(summary["read_mbs_avg"])
        self.assertIsNone(summary["load_source"])


class ParseFioLogsTests(unittest.TestCase):
    def test_parses_bw_and_iops_logs_and_deletes_files(self):
        tmp = Path(tempfile.mkdtemp())
        # 12000000 KiB/s = 12000000 * 1024 / 1e6 = 12288 МБ/с
        (tmp / "fio-nvme1n1-seq_read_bw.0.log").write_text(
            "1754400000000, 12000000, 0, 0\n"
            "1754400001000, 12000000, 0, 0\n",
            encoding="utf-8",
        )
        (tmp / "fio-nvme1n1-seq_read_iops.0.log").write_text(
            "1754400000000, 48000, 0, 0\n"
            "1754400001000, 48000, 0, 0\n",
            encoding="utf-8",
        )

        prefix = str(tmp / "fio-nvme1n1-seq_read")
        data = parse_fio_logs(prefix)

        self.assertEqual(data[1754400000]["read_mbs"], 12288.0)
        self.assertEqual(data[1754400000]["write_mbs"], 0.0)
        self.assertEqual(data[1754400000]["iops"], 48000)
        self.assertEqual(data[1754400001]["read_mbs"], 12288.0)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_sums_across_job_files_and_write_direction(self):
        tmp = Path(tempfile.mkdtemp())
        for job in (0, 1):
            (tmp / f"fio-nvme1n1-rand_write_bw.{job}.log").write_text(
                "1754400000000, 500000, 1, 0\n",
                encoding="utf-8",
            )
        (tmp / "fio-nvme1n1-rand_write_iops.0.log").write_text(
            "1754400000000, 100000, 1, 0\n",
            encoding="utf-8",
        )

        prefix = str(tmp / "fio-nvme1n1-rand_write")
        data = parse_fio_logs(prefix)

        row = data[1754400000]
        self.assertAlmostEqual(row["write_mbs"], 500000 * 2 * 1024 / 1e6, places=1)
        self.assertEqual(row["read_mbs"], 0.0)
        self.assertEqual(row["iops"], 100000)

    def test_none_when_no_log_files(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertIsNone(parse_fio_logs(str(tmp / "missing-prefix")))

    def test_skips_malformed_and_zero_rows(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "fio-x_bw.0.log").write_text(
            "not-a-number, 1, 0\n"
            "1754400000000, 0, 0\n"
            "1754400000000, 1000, 0\n",
            encoding="utf-8",
        )
        data = parse_fio_logs(str(tmp / "fio-x"))
        self.assertAlmostEqual(data[1754400000]["read_mbs"], 1000 * 1024 / 1e6)

    def test_iops_normalized_when_log_in_x1000_units(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "fio-x_bw.0.log").write_text(
            "1754400000000, 1000000, 0, 0\n",
            encoding="utf-8",
        )
        (tmp / "fio-x_iops.0.log").write_text(
            "1754400000000, 500000000, 0, 0\n",
            encoding="utf-8",
        )
        data = parse_fio_logs(str(tmp / "fio-x"))
        self.assertEqual(data[1754400000]["iops"], 500000)

    def test_merge_fio_logs_matches_samples_by_timestamp(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "fio-nvme1n1-seq_read_bw.0.log").write_text(
            "1754400001000, 12000000, 0, 0\n",
            encoding="utf-8",
        )
        (tmp / "fio-nvme1n1-seq_read_iops.0.log").write_text(
            "1754400001000, 48000, 0, 0\n",
            encoding="utf-8",
        )

        with _patch_paths(tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None):
            sampler = DiagnosticSampler(DISK)
        sampler.samples = [
            {"ts": 1754400000.4, "read_mbs": None, "write_mbs": None, "iops": None},
            {"ts": 1754400001.2, "read_mbs": None, "write_mbs": None, "iops": None},
        ]
        merged = sampler.merge_fio_logs(str(tmp / "fio-nvme1n1-seq_read"))

        self.assertTrue(merged)
        self.assertEqual(sampler.samples[1]["read_mbs"], 12288.0)
        self.assertEqual(sampler.samples[1]["iops"], 48000)
        self.assertEqual(sampler.samples[1]["load_source"], "fio")
        self.assertIsNone(sampler.samples[0]["read_mbs"])

    def test_merge_returns_false_without_logs(self):
        tmp = Path(tempfile.mkdtemp())
        sampler = DiagnosticSampler(DISK)
        sampler.samples = []
        self.assertFalse(sampler.merge_fio_logs(str(tmp / "no-such-prefix")))


class CollectStaticInfoTests(unittest.TestCase):
    def test_numa_node_read_from_device_dir(self):
        tmp = Path(tempfile.mkdtemp())
        dev = tmp / "sys" / "class" / "nvme" / "nvme1" / "device"
        dev.mkdir(parents=True)
        (dev / "numa_node").write_text("1", encoding="utf-8")

        with _patch_paths(tmp):
            info = collect_static_info(DISK)

        self.assertEqual(info["numa_node"], "1")


if __name__ == "__main__":
    unittest.main()
