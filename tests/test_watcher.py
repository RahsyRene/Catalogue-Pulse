from __future__ import annotations

import json
import unittest
from pathlib import Path

import watcher


SETTINGS = json.loads(Path("settings.json").read_text(encoding="utf-8"))


class FilterTests(unittest.TestCase):
    def test_utility_is_not_mistaken_for_ut(self) -> None:
        name = "Utility Parka"
        category = watcher.category_for(name, SETTINGS)
        self.assertEqual(category, "jackets")
        self.assertIsNone(watcher.exclusion_reason(name, category, SETTINGS))

    def test_ut_tshirt_is_excluded(self) -> None:
        name = "PEANUTS UT T-Shirt"
        category = watcher.category_for(name, SETTINGS)
        self.assertEqual(category, "tops")
        self.assertIsNotNone(watcher.exclusion_reason(name, category, SETTINGS))

    def test_shorts_are_excluded(self) -> None:
        name = "AIRism Shorts"
        category = watcher.category_for(name, SETTINGS)
        self.assertIsNotNone(watcher.exclusion_reason(name, category, SETTINGS))

    def test_slim_fit_pants_are_excluded(self) -> None:
        name = "Stretch Jeans Slim Fit"
        category = watcher.category_for(name, SETTINGS)
        self.assertEqual(category, "pants")
        self.assertIsNotNone(watcher.exclusion_reason(name, category, SETTINGS))

    def test_overshirt_is_jacket(self) -> None:
        self.assertEqual(
            watcher.category_for("Baumwolle Overshirt", SETTINGS),
            "jackets",
        )


class EventTests(unittest.TestCase):
    def test_price_drop_and_new_size(self) -> None:
        previous = {
            "E1": {
                "active": True,
                "price": 19.90,
                "sizes": ["S"],
            }
        }
        current = {
            "E1": {
                "product_id": "E1",
                "name": "Test",
                "category": "tops",
                "price": 14.90,
                "original_price": 29.90,
                "discount": 50,
                "sizes": ["S", "M"],
                "url": "https://example.com",
                "image_url": "",
                "active": True,
                "last_seen": "2026-01-01T00:00:00+00:00",
            }
        }
        events = watcher.detect_events(current, previous)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "changed")
        self.assertEqual(len(events[0]["reasons"]), 2)


if __name__ == "__main__":
    unittest.main()
