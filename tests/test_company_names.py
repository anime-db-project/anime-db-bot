from __future__ import annotations

import unittest
from core.company_names import dedupe_company_names, company_identity_key
from core.models import AnimeRecord
from services.aggregation.merge import _merge_studios


class CompanyNamesTests(unittest.TestCase):
    def test_pierrot_historical_and_registered_aliases(self):
        labels = [
            "Pierrot Co., Ltd.",
            "Studio pierrot",
            "StudioPierrot",
            "Studio-Pierrot",
            "PIERROT",
            "株式会社ぴえろ",
            "株式会社スタジオぴえろ",
            "Ｓｔｕｄｉｏ Ｐｉｅｒｒｏｔ Co.,Ltd.",
        ]
        self.assertEqual(dedupe_company_names(labels), ["Studio Pierrot"])
        self.assertEqual(
            dedupe_company_names(list(reversed(labels))), ["Studio Pierrot"]
        )

    def test_eightbit_aliases(self):
        self.assertEqual(
            dedupe_company_names(
                ["Eightbit Co., Ltd.", "8bit", "8 Bit", "株式会社エイトビット"]
            ),
            ["8bit"],
        )

    def test_legal_forms_and_preferred_display(self):
        self.assertEqual(
            dedupe_company_names(
                [
                    "VIZ Media, L.L.C.",
                    "VIZ Media",
                    "viz  media",
                    "Muse Communication Co.,Ltd.",
                    "MUSE COMMUNICATION LTD.",
                ]
            ),
            ["VIZ Media", "Muse Communication Co.,Ltd."],
        )

    def test_distinct_studios_and_non_latin_names_remain_distinct(self):
        names = [
            "Studio Pierrot",
            "Pierrot Films",
            "Pierrot Plus",
            "Toei",
            "Toei Animation",
            "Madhouse",
            "Madhouse Pictures",
            "株式会社甲",
            "株式会社乙",
            "A B",
            "AB",
        ]
        self.assertEqual(dedupe_company_names(names), names)
        self.assertNotEqual(
            company_identity_key("A-1 Pictures"),
            company_identity_key("A1 Picture Works"),
        )

    def test_aggregation_uses_same_company_filter(self):
        records = [
            AnimeRecord(source="AniList", source_id="1", studios=["Pierrot", "8bit"]),
            AnimeRecord(
                source="MAL",
                source_id="1",
                studios=[
                    "Studio Pierrot Co.,Ltd.",
                    "Eightbit Co., Ltd.",
                    "Pierrot Films",
                ],
            ),
        ]
        self.assertEqual(
            _merge_studios(records), ["Studio Pierrot", "8bit", "Pierrot Films"]
        )
        self.assertEqual(records[0].studios, ["Pierrot", "8bit"])
