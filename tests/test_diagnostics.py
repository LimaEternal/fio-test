import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.diagnostics import DiagnosticSampler, collect_static_info


DISK = {
    "name": "nvme1n1", "path": "/dev/nvme1n1", "tran": "NVME",
}


def _patch_paths(tmp):
    """Перенаправляет /proc/diskstats, /sys/class/nvme и /sys/class/block
    на временный каталог."""
    root = str(tmp)

    def _mapped(path):
        p = str(path)
        if p.startswith("/proc/diskstats"):
            return Path(root) / "proc" / "diskstats"
        if p.startswith("/sys/class/nvme") or p.startswith("/sys/class/block"):
            return Path(root) / "sys" / p[len("/sys"):].lstrip("/")
        return Path(p)

    return mock.patch("utils.diagnostics.Path", side_effect=_mapped)


class DiagnosticSamplerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Линк, температура и NUMA-нода
        dev = self.tmp / "sys" / "class" / "nvme" / "nvme1" / "device"
        dev.mkdir(parents=True)
        (dev / "current_link_speed").write_text("32.0 GT/s PCIe", encoding="utf-8")
        (dev / "current_link_width").write_text("x4", encoding="utf-8")
        (dev / "numa_node").write_text("1", encoding="utf-8")
        hwmon = self.tmp / "sys" / "class" / "nvme" / "nvme1" / "hwmon" / "hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "temp1_input").write_text("41000", encoding="utf-8")
        # Нагрузка на диск
        proc = self.tmp / "proc"
        proc.mkdir(parents=True, exist_ok=True)
        self.diskstats_cur = (
            "259 3 nvme1n1 100000 0 2000000 0 50000 0 4000000 0 0 0 1000000\n"
        )
        (proc / "diskstats").write_text(self.diskstats_cur, encoding="utf-8")

        self.link_dir = dev

    def test_link_and_temperature_are_read(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=self.link_dir):
            sampler = DiagnosticSampler(DISK)
            self.assertEqual(sampler._read_link(), (32.0, 4))
            self.assertEqual(sampler._read_temp(), 41.0)

    def test_diskstats_deltas_and_summary(self):
        with _patch_paths(self.tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=self.link_dir):
            sampler = DiagnosticSampler(DISK)
            sampler._prev_diskstats = {
                "reads": 0, "sectors_read": 0,
                "writes": 0, "sectors_written": 0, "weighted_io": 0,
            }
            sampler._prev_ts = time.time() - 1.0
            sampler._sample_once()
            summary = sampler.summary()

        self.assertAlmostEqual(summary["read_mbs_avg"], 1024.0, delta=15)
        self.assertAlmostEqual(summary["write_mbs_avg"], 2048.0, delta=15)
        self.assertAlmostEqual(summary["iops_avg"], 150000, delta=3000)
        self.assertAlmostEqual(summary["avgqu_sz_max"], 1000.0, delta=15)
        self.assertEqual(summary["link_gts_min"], 32.0)
        self.assertEqual(summary["link_width_min"], 4)
        self.assertEqual(summary["temp_max_c"], 41.0)
        self.assertEqual(summary["sources"]["link"], True)
        self.assertEqual(summary["sources"]["temp"], True)
        self.assertEqual(summary["sources"]["diskstats"], True)
        self.assertIsNotNone(summary["diskstats_first"])

    def test_diskstats_matched_by_major_minor(self):
        tmp = Path(tempfile.mkdtemp())
        blk = tmp / "sys" / "class" / "block" / "nvme1n1"
        blk.mkdir(parents=True)
        (blk / "dev").write_text("259:3", encoding="utf-8")
        proc = tmp / "proc"
        proc.mkdir(parents=True)
        # Имя в /proc/diskstats не совпадает, но major:minor совпадает.
        (proc / "diskstats").write_text(
            "259 3 nvmeX 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
            encoding="utf-8",
        )

        with _patch_paths(tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None):
            sampler = DiagnosticSampler(DISK)
            cur = sampler._read_diskstats()

        self.assertIsNotNone(cur)
        self.assertEqual(cur["reads"], 0)

    def test_temp_read_from_block_hwmon_fallback(self):
        tmp = Path(tempfile.mkdtemp())
        hwmon = tmp / "sys" / "class" / "block" / "nvme1n1" / "device" / "hwmon" / "hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "temp1_input").write_text("38000", encoding="utf-8")

        with _patch_paths(tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None):
            sampler = DiagnosticSampler(DISK)
            temp = sampler._read_temp()

        self.assertEqual(temp, 38.0)

    def test_source_status_and_none_values_when_sources_missing(self):
        tmp = Path(tempfile.mkdtemp())
        proc = tmp / "proc"
        proc.mkdir(parents=True)
        (proc / "diskstats").write_text(
            "259 3 nvme1n1 10 0 1000 0 20 0 2000 0 0 0 3000\n",
            encoding="utf-8",
        )

        with _patch_paths(tmp), \
             mock.patch("utils.diagnostics.find_nvme_link_dir", return_value=None):
            sampler = DiagnosticSampler(DISK)
            sampler._sample_once()
            sample = sampler.samples[0]

        self.assertEqual(sample["gts"], None)
        self.assertEqual(sample["temp"], None)
        self.assertIsNone(sample["read_mbs"])
        self.assertIsNone(sample["write_mbs"])
        self.assertEqual(sampler.source_status,
                         {"link": False, "temp": False, "diskstats": True})

        summary = sampler.summary()
        self.assertEqual(summary["sources"]["diskstats"], True)
        self.assertEqual(summary["sources"]["temp"], False)
        self.assertIsNone(summary["read_mbs_avg"])
        self.assertIsNotNone(summary["diskstats_first"])
        self.assertEqual(summary["diskstats_first"]["reads"], 10)


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
