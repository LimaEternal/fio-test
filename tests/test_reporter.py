import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.reporter import _render_sampler_tables, _render_source_notes, generate_report

DISK = {
    "name": "nvme1n1", "path": "/dev/nvme1n1", "model": "KIOXIA KCMY1VUG3T20",
    "serial": "SN", "tran": "NVME", "size": "3.2T", "phy_sec": 4096,
    "profile": {
        "interface": "nvme", "physical_block_size": 4096, "logical_block_size": 4096,
        "rotational": 0, "ceiling_mbps": 15753.6,
        "link": {"gen": 5, "width": 4, "speed_gts": 32.0,
                 "max_gen": 5, "max_width": 4, "max_speed_gts": 32.0,
                 "source": "sysfs"},
    },
}

TEST_NAMES = {
    "seq_read": "1. Послед. Чтение",
    "seq_write": "2. Послед. Запись",
    "rand_read": "3. Случ. Чтение 4k",
    "rand_write": "4. Случ. Запись 4k",
}

SAMPLE = {
    "gts": 32.0, "width": 4, "temp": 41.0,
    "read_mbs": 12400.5, "write_mbs": 0.0, "iops": 47694,
}


def make_diag_store(samples):
    return {
        "nvme1n1": {
            "seq_read": {
                "samples": samples,
                "summary": {
                    "link_gts_min": 32.0, "link_width_min": 4, "temp_max_c": 41.0,
                    "read_mbs_avg": 12400.5, "write_mbs_avg": 0.0, "iops_avg": 47694,
                    "samples": len(samples),
                },
            }
        }
    }


def make_results(include_diag=True):
    res = {
        "iops": 47694, "bw_mb": 12400.5, "lat_avg": 1.33, "lat_p99": 1.83,
        "cpu_user": 2.0, "cpu_sys": 16.0, "status": "ok",
        "clat_p50_ms": 1.2, "clat_p90_ms": 1.5, "clat_p99_ms": 1.83,
        "clat_p99_9_ms": 2.1, "slat_avg_ms": 0.002, "iodepth": 16, "io_kb": 500000,
    }
    seq_read = dict(res)
    if include_diag:
        seq_read["diag"] = {"temp_max_c": 41.0, "temp_avg_c": 39.5}
    return [
        {"_thresholds": {}, "seq_read": seq_read},
    ]


class RenderSamplerTablesTests(unittest.TestCase):
    def test_renders_per_second_rows(self):
        samples = [SAMPLE, {**SAMPLE, "temp": 42.0, "iops": 47700}]
        lines = _render_sampler_tables(make_diag_store(samples), "nvme1n1", TEST_NAMES)

        text = "\n".join(lines)
        self.assertIn("Сэмплы линка/температуры/нагрузки", text)
        self.assertIn("| Сек | Линк | t°C |", text)
        self.assertIn("| 1 | 32 GT/s x4 | 41.0 | 12400.5 | 0.0 | 47,694 |", text)
        self.assertIn("| 2 | 32 GT/s x4 | 42.0 | 12400.5 | 0.0 | 47,700 |", text)

    def test_unknown_disk_renders_nothing(self):
        lines = _render_sampler_tables(make_diag_store([SAMPLE]), "nvmeX", TEST_NAMES)
        self.assertEqual(lines, [])

    def test_none_values_rendered_as_dash(self):
        sample = {"gts": None, "width": None, "temp": None,
                  "read_mbs": None, "write_mbs": None, "iops": None}
        lines = _render_sampler_tables(make_diag_store([sample]), "nvme1n1", TEST_NAMES)
        text = "\n".join(lines)
        self.assertIn("| 1 | — | — | — | — | — |", text)


class RenderSourceNotesTests(unittest.TestCase):
    def test_notes_when_sources_missing(self):
        disk_results = {
            "_thresholds": {},
            "seq_read": {
                "iops": 1, "bw_mb": 1, "lat_avg": 0, "lat_p99": 0,
                "cpu_user": 0, "cpu_sys": 0, "status": "ok",
                "diag": {
                    "sources": {"link": False, "temp": False},
                },
            },
        }
        lines = _render_source_notes(disk_results, TEST_NAMES)
        text = "\n".join(lines)
        self.assertIn("температура недоступна", text)
        self.assertIn("линк PCIe", text)

    def test_no_notes_when_all_sources_available(self):
        disk_results = {
            "_thresholds": {},
            "seq_read": {
                "iops": 1, "bw_mb": 1, "lat_avg": 0, "lat_p99": 0,
                "cpu_user": 0, "cpu_sys": 0, "status": "ok",
                "diag": {
                    "sources": {"link": True, "temp": True},
                },
            },
        }
        lines = _render_source_notes(disk_results, TEST_NAMES)
        self.assertEqual(lines, [])

    def test_notes_from_diag_notes_are_rendered(self):
        disk_results = {
            "_thresholds": {},
            "seq_read": {
                "iops": 1, "bw_mb": 1, "lat_avg": 0, "lat_p99": 0,
                "cpu_user": 0, "cpu_sys": 0, "status": "ok",
                "diag": {
                    "notes": [
                        "температура недоступна: установите nvme-cli (нужен nvme smart-log)",
                    ],
                },
            },
        }
        lines = _render_source_notes(disk_results, TEST_NAMES)
        text = "\n".join(lines)
        self.assertIn("> температура недоступна: установите nvme-cli", text)

    def test_notes_deduplicated_across_tests(self):
        diag = {"notes": ["линк PCIe не удалось прочитать"]}
        disk_results = {
            "_thresholds": {},
            "seq_read": {"iops": 1, "bw_mb": 1, "lat_avg": 0, "lat_p99": 0,
                         "cpu_user": 0, "cpu_sys": 0, "status": "ok",
                         "diag": dict(diag)},
            "rand_read": {"iops": 1, "bw_mb": 1, "lat_avg": 0, "lat_p99": 0,
                          "cpu_user": 0, "cpu_sys": 0, "status": "ok",
                          "diag": dict(diag)},
        }
        lines = _render_source_notes(disk_results, TEST_NAMES)
        self.assertEqual(lines.count("> линк PCIe не удалось прочитать"), 1)

    def test_note_when_p99_unreliable(self):
        disk_results = {
            "_thresholds": {},
            "seq_read": {
                "iops": 1, "bw_mb": 1, "lat_avg": 0.6, "lat_p99": 17112.76,
                "lat_p99_unreliable": True,
                "cpu_user": 0, "cpu_sys": 0, "status": "ok",
            },
        }
        lines = _render_source_notes(disk_results, TEST_NAMES)
        text = "\n".join(lines)
        self.assertIn("недостоверны", text)
        self.assertIn("reports/raw", text)

    def test_no_note_when_p99_reliable(self):
        disk_results = {
            "_thresholds": {},
            "seq_read": {
                "iops": 1, "bw_mb": 1, "lat_avg": 0.6, "lat_p99": 1.83,
                "cpu_user": 0, "cpu_sys": 0, "status": "ok",
            },
        }
        self.assertEqual(_render_source_notes(disk_results, TEST_NAMES), [])


class GenerateReportDiagStoreTests(unittest.TestCase):
    def test_report_contains_sampler_tables_with_diag_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                diag_store=make_diag_store([SAMPLE]),
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("Сэмплы линка/температуры/нагрузки", text)
        self.assertIn("32 GT/s x4", text)
        self.assertIn("**Мониторинг — nvme1n1**", text)

    def test_report_without_diag_store_has_no_diag_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(include_diag=False), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertNotIn("Сэмплы линка/температуры/нагрузки", text)
        self.assertNotIn("**Мониторинг —", text)
        self.assertNotIn("| NUMA |", text)
        self.assertIn("## Результаты тестирования", text)

    def test_report_without_diag_store_shows_block_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(include_diag=False), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("Блок (физ/лог)", text)
        self.assertIn("4096/4096", text)
        self.assertNotIn("| NUMA |", text)

    def test_report_survives_wall_s_float_in_results(self):
        results = [
            {
                "_thresholds": {},
                "_wall_s": 302.0,
                "seq_read": {
                    "iops": 44050, "bw_mb": 5773.8, "lat_avg": 0.73,
                    "lat_p99": 1.2, "cpu_user": 2.0, "cpu_sys": 16.0,
                    "status": "ok",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], results, TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("## Результаты тестирования", text)
        self.assertIn("— тесты: 5 мин 02 с", text)

    def test_report_survives_string_test_values(self):
        disk_results = {
            test_id: {"bs": "test", "iops": "test", "bw_mb": "test",
                      "lat_avg": "test", "lat_p99": "test", "status": "test"}
            for test_id in TEST_NAMES
        }
        disk_results["_thresholds"] = {}
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], [disk_results], TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("## Сводка", text)
        self.assertIn("test", text)


class GenerateReportRunInfoTests(unittest.TestCase):
    def test_report_contains_run_info_command_and_flags(self):
        run_info = {
            "command": "python fio-test.py -l -s",
            "flags": [
                ("Режим", "последовательный"),
                ("Подробные логи", "включены"),
                ("Длительность теста", "60 сек"),
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                run_info=run_info,
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("## Параметры запуска", text)
        self.assertIn("python fio-test.py -l -s", text)
        self.assertIn("| Параметр | Значение |", text)
        self.assertIn("| Режим | последовательный |", text)
        self.assertIn("| Длительность теста | 60 сек |", text)

    def test_report_without_run_info_has_no_params_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertNotIn("## Параметры запуска", text)


class GenerateReportTestPlansTests(unittest.TestCase):
    def test_report_contains_actual_test_params(self):
        test_plans = {
            "nvme0n1": {
                "interface": "nvme",
                "ceiling_mbps": 15753.6,
                "max_sectors_kb": 1280,
                "target_iops": 50000,
                "tests": {
                    "seq_read": {"bs": "256k", "iodepth": 64, "numjobs": 2},
                    "seq_write": {"bs": "256k", "iodepth": 64, "numjobs": 2},
                    "rand_read": {"bs": "4k", "iodepth": 16, "numjobs": 16},
                    "rand_write": {"bs": "4k", "iodepth": 16, "numjobs": 16},
                },
                "thresholds": {
                    "seq_read": {"min_bw_mb": 12477.0},
                    "seq_write": {"min_bw_mb": 6612.8},
                    "rand_read": {"min_iops": 500000},
                    "rand_write": {"min_iops": 200000},
                },
                "threshold_source": {
                    "seq_read": "sysfs (формула)",
                    "seq_write": "sysfs (формула)",
                    "rand_read": "конфиг (thresholds.json)",
                    "rand_write": "конфиг (thresholds.json)",
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                test_plans=test_plans,
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("## Фактические параметры тестов", text)
        self.assertIn("**/dev/nvme0n1**", text)
        self.assertIn("потолок шины 15754 МБ/с", text)
        self.assertIn("256k", text)
        self.assertIn("2", text)
        self.assertIn("**Пороги PASS/FAIL:**", text)
        self.assertIn("12477 МБ/с", text)
        self.assertIn("6613 МБ/с", text)
        self.assertIn("500,000 IOPS", text)
        self.assertIn("sysfs (формула)", text)
        self.assertIn("конфиг (thresholds.json)", text)

    def test_report_without_test_plans_has_no_config_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertNotIn("## Фактические параметры тестов", text)


class GenerateReportLatP99Tests(unittest.TestCase):
    def test_lat_p99_column_shown_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                show_lat_p99=True,
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("Lat P99 (мс)", text)
        self.assertIn("1.83", text)

    def test_lat_p99_column_hidden_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertNotIn("Lat P99 (мс)", text)

    def test_unreliable_p99_rendered_as_dash_and_noted(self):
        res = make_results()[0]
        res["seq_read"]["lat_p99"] = 17112.76
        res["seq_read"]["lat_avg"] = 0.6
        res["seq_read"]["lat_p99_unreliable"] = True

        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], [res], TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                show_lat_p99=True,
                diag_store={},
            )
            text = path.read_text(encoding="utf-8")

        self.assertNotIn("17112.76", text)
        self.assertIn("недостоверны", text)


class RenderSamplerTablesRampTests(unittest.TestCase):
    def test_ramp_rows_without_load_skipped_when_fio_source(self):
        samples = [
            {"ts": 1.0, "gts": 32.0, "width": 4, "temp": 40.0,
             "read_mbs": None, "write_mbs": None, "iops": None},
            {"ts": 2.0, "gts": 32.0, "width": 4, "temp": 40.0,
             "read_mbs": 12400.5, "write_mbs": 0.0, "iops": 47694},
        ]
        store = {
            "nvme1n1": {
                "seq_read": {
                    "samples": samples,
                    "summary": {"load_source": "fio"},
                }
            }
        }
        lines = _render_sampler_tables(
            store, "nvme1n1", {"seq_read": "1. Послед. Чтение"}
        )
        text = "\n".join(lines)
        self.assertNotIn("| 1 | 32 GT/s x4 | 40.0 | — |", text)
        self.assertIn("| 2 | 32 GT/s x4 | 40.0 | 12400.5 | 0.0 | 47,694 |", text)

    def test_empty_rows_kept_when_no_fio_load_source(self):
        samples = [{"ts": 1.0, "gts": 32.0, "width": 4, "temp": 40.0,
                    "read_mbs": None, "write_mbs": None, "iops": None}]
        store = {
            "nvme1n1": {
                "seq_read": {"samples": samples, "summary": {"load_source": None}},
            }
        }
        lines = _render_sampler_tables(
            store, "nvme1n1", {"seq_read": "1. Послед. Чтение"}
        )
        text = "\n".join(lines)
        self.assertIn("| 1 | 32 GT/s x4 | 40.0 | — | — | — |", text)


class RenderSummaryTests(unittest.TestCase):
    def test_summary_renders_status_and_metric_per_disk(self):
        results = [{
            "_thresholds": {},
            "seq_read": {"iops": 47694, "bw_mb": 12400.5, "status": "PASS"},
            "seq_write": {"iops": 10000, "bw_mb": 3000.0, "status": "PASS"},
            "rand_read": {"iops": 1000000, "bw_mb": 0, "status": "PASS"},
            "rand_write": {"iops": 0, "bw_mb": 0, "status": "FAIL"},
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], results, TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("## Сводка", text)
        self.assertIn("PASS 12400 МБ/с", text)
        self.assertIn("PASS 1.00M IOPS", text)
        self.assertIn("| FAIL |", text)

    def test_summary_error_test_shown_as_fail(self):
        results = [{"_thresholds": {}, "seq_read": {"error": "boom"}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], results, TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("## Сводка", text)
        self.assertIn("| FAIL |", text)


class GenerateReportShowTmaxTests(unittest.TestCase):
    def test_plain_report_has_tmax_column_but_no_monitoring_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                show_tmax=True,
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("Tmax (°C)", text)
        self.assertIn("Tavg (°C)", text)
        self.assertIn("| 41.0 |", text)
        self.assertIn("| 39.5 |", text)
        self.assertNotIn("**Мониторинг —", text)
        self.assertNotIn("| NUMA |", text)

    def test_plain_report_tmax_dash_without_diag_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(include_diag=False), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
                show_tmax=True,
            )
            text = path.read_text(encoding="utf-8")

        self.assertIn("Tmax (°C)", text)
        self.assertIn("Tavg (°C)", text)
        self.assertNotIn("**Мониторинг —", text)

    def test_plain_report_without_tmax_flag_has_no_tmax_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = generate_report(
                [DISK], make_results(), TEST_NAMES,
                output_path=Path(tmp) / "report.md",
            )
            text = path.read_text(encoding="utf-8")

        self.assertNotIn("Tmax (°C)", text)
        self.assertNotIn("Tavg (°C)", text)
        self.assertNotIn("**Мониторинг —", text)


if __name__ == "__main__":
    unittest.main()
