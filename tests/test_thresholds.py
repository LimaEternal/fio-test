import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.thresholds import (
    KNOWN_TESTS,
    load_base_thresholds,
    load_disk_thresholds,
    normalize_model,
    resolve_thresholds,
    validate_base_thresholds,
    validate_disk_thresholds,
)


def _base():
    return {
        "nvme": {
            "gen3": {"seq_read": {"min_bw_mb": 3000}, "seq_write": {"min_bw_mb": 1500},
                     "rand_read": {"min_iops": 300000}, "rand_write": {"min_iops": 100000}},
            "gen4": {"seq_read": {"min_bw_mb": 5000}, "seq_write": {"min_bw_mb": 3000},
                     "rand_read": {"min_iops": 500000}, "rand_write": {"min_iops": 200000}},
            "gen5": {"seq_read": {"min_bw_mb": 6500}, "seq_write": {"min_bw_mb": 4000},
                     "rand_read": {"min_iops": 700000}, "rand_write": {"min_iops": 250000}},
        },
        "sas": {"12g": {"seq_read": {"min_bw_mb": 800}, "seq_write": {"min_bw_mb": 600},
                        "rand_read": {"min_iops": 30000}, "rand_write": {"min_iops": 25000}}},
        "sata": {"6g": {"seq_read": {"min_bw_mb": 400}, "seq_write": {"min_bw_mb": 300},
                        "rand_read": {"min_iops": 10000}, "rand_write": {"min_iops": 8000}}},
        "hdd": {"any": {"seq_read": {"min_bw_mb": 150}, "seq_write": {"min_bw_mb": 150},
                        "rand_read": {"min_iops": 2000}, "rand_write": {"min_iops": 1000}}},
    }


def _disk(iface, model="GENERIC SSD", rotational=0, link=None):
    return {
        "name": "sdX", "model": model,
        "tran": iface,
        "profile": {"interface": iface, "rotational": rotational, "link": link},
    }


class NormalizeModelTests(unittest.TestCase):
    def test_case_and_whitespace(self):
        self.assertEqual(normalize_model("  samsung   PM883 "), "SAMSUNG PM883")

    def test_tabs_and_newlines_collapsed(self):
        self.assertEqual(normalize_model("a\tb\nc  d"), "A B C D")

    def test_none_and_empty(self):
        self.assertEqual(normalize_model(None), "")
        self.assertEqual(normalize_model("   "), "")


class LoaderTests(unittest.TestCase):
    def test_loaders_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_p = Path(tmp) / "base.json"
            disk_p = Path(tmp) / "disks.json"
            base_p.write_text('{"sata": {"6g": {"seq_read": {"min_bw_mb": 1}}}}',
                              encoding="utf-8")
            disk_p.write_text('{"X": {"rand_read": {"min_iops": 5}}}', encoding="utf-8")
            self.assertEqual(
                load_base_thresholds(base_p)["sata"]["6g"]["seq_read"]["min_bw_mb"], 1
            )
            self.assertIn("X", load_disk_thresholds(disk_p))

    def test_load_broken_json_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "broken.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_base_thresholds(p)

    def test_load_missing_file_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_base_thresholds(Path(tmp) / "nope.json")


class ValidateBaseTests(unittest.TestCase):
    def test_valid_full_base_passes(self):
        validate_base_thresholds(_base())

    def test_unknown_interface_rejected(self):
        base = _base()
        base["scsi"] = base["sata"]
        with self.assertRaisesRegex(ValueError, "scsi"):
            validate_base_thresholds(base)

    def test_missing_test_in_row_rejected(self):
        base = _base()
        del base["sata"]["6g"]["rand_write"]
        with self.assertRaisesRegex(ValueError, "rand_write"):
            validate_base_thresholds(base)

    def test_negative_metric_rejected(self):
        base = _base()
        base["sata"]["6g"]["seq_read"] = {"min_bw_mb": -5}
        with self.assertRaisesRegex(ValueError, "больше нуля"):
            validate_base_thresholds(base)

    def test_string_metric_rejected(self):
        base = _base()
        base["sata"]["6g"]["seq_read"] = {"min_bw_mb": "много"}
        with self.assertRaisesRegex(ValueError, "числом"):
            validate_base_thresholds(base)

    def test_metric_without_key_rejected(self):
        base = _base()
        base["sata"]["6g"]["seq_read"] = {"min_gbps": 1}
        with self.assertRaisesRegex(ValueError, "min_bw_mb"):
            validate_base_thresholds(base)


class ValidatePersonalTests(unittest.TestCase):
    def test_partial_entry_is_valid(self):
        validate_disk_thresholds({"SAMSUNG PM883": {"seq_read": {"min_bw_mb": 500}}})

    def test_empty_dict_is_valid(self):
        validate_disk_thresholds({})

    def test_unknown_test_rejected(self):
        with self.assertRaisesRegex(ValueError, "seqread"):
            validate_disk_thresholds({"X": {"seqread": {"min_bw_mb": 100}}})

    def test_empty_model_rejected(self):
        with self.assertRaisesRegex(ValueError, "пустой ключ"):
            validate_disk_thresholds({"   ": {"seq_read": {"min_bw_mb": 1}}})

    def test_zero_metric_rejected(self):
        with self.assertRaisesRegex(ValueError, "больше нуля"):
            validate_disk_thresholds({"X": {"rand_read": {"min_iops": 0}}})


class ResolveThresholdsTests(unittest.TestCase):
    """Выбор порогов: персональные -> hdd -> интерфейс+поколение."""

    def test_personal_match_normalized_model(self):
        disk = _disk("sata", model="  samsung   pm883 ")
        personal = {"SAMSUNG PM883": {"seq_read": {"min_bw_mb": 555}}}
        thr, src = resolve_thresholds(disk, _base(), personal)
        self.assertEqual(thr, {"seq_read": {"min_bw_mb": 555}})
        self.assertEqual(src, {"seq_read": "персональные (по модели)"})

    def test_personal_applied_whole_not_merged(self):
        disk = _disk("sata", model="X")
        personal = {"X": {"seq_read": {"min_bw_mb": 555}}}
        thr, _ = resolve_thresholds(disk, _base(), personal)
        # Недостающие тесты не добираются из общего файла
        self.assertNotIn("rand_read", thr)

    def test_personal_miss_uses_base_sata_row(self):
        disk = _disk("sata", model="UNKNOWN", link={"spd_limit_gbps": 6.0})
        thr, src = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 400})
        self.assertEqual(set(thr), set(KNOWN_TESTS))
        for tid in thr:
            self.assertEqual(src[tid], "общие (sata 6g)")

    def test_sas_single_row(self):
        disk = _disk("sas", link={"negotiated_gbps": 12.0})
        thr, src = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 800})
        self.assertEqual(src["seq_read"], "общие (sas 12g)")

    def test_nvme_gen4_row_selected(self):
        disk = _disk("nvme", link={"width": 4, "speed_gts": 16.0})
        thr, src = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 5000})
        self.assertEqual(src["seq_read"], "общие (nvme gen4)")

    def test_nvme_gen_clamped_to_top_row(self):
        disk = _disk("nvme", link={"width": 4, "speed_gts": 64.0})  # Gen6 > gen5
        thr, _ = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 6500})

    def test_nvme_gen_below_rows_uses_lowest(self):
        disk = _disk("nvme", link={"width": 2, "speed_gts": 5.0})  # Gen2 < gen3
        thr, _ = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 3000})

    def test_nvme_without_link_uses_lowest_row(self):
        disk = _disk("nvme", link=None)
        thr, src = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 3000})
        self.assertEqual(src["seq_read"], "общие (nvme gen3)")

    def test_hdd_section_overrides_interface(self):
        disk = _disk("sata", model="WDC WD4004FZWX", rotational=1,
                     link={"spd_limit_gbps": 6.0})
        thr, src = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 150})
        self.assertEqual(src["seq_read"], "общие (hdd)")

    def test_hdd_personal_wins_over_hdd_section(self):
        disk = _disk("sas", model="ST1800MM0129", rotational=1)
        personal = {"ST1800MM0129": {"seq_read": {"min_bw_mb": 222}}}
        thr, src = resolve_thresholds(disk, _base(), personal)
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 222})
        self.assertEqual(src["seq_read"], "персональные (по модели)")

    def test_no_section_returns_empty(self):
        disk = _disk("sas")
        thr, src = resolve_thresholds(disk, {}, {})
        self.assertEqual((thr, src), ({}, {}))

    def test_profile_interface_preferred_over_tran(self):
        disk = {"name": "sdX", "model": "U", "tran": "sata",
                "profile": {"interface": "sas", "rotational": 0,
                            "link": {"negotiated_gbps": 12.0}}}
        thr, _ = resolve_thresholds(disk, _base(), {})
        self.assertEqual(thr["seq_read"], {"min_bw_mb": 800})


if __name__ == "__main__":
    unittest.main()
