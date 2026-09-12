import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "telegram_collector_free.py"
SPEC = importlib.util.spec_from_file_location("telegramnewsai_collector", SOURCE)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class OfflineRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        collector.APP_DIR = self.directory
        collector.DB_FILE = self.directory / "news.db"
        self.connection = collector.open_db()
        self.addCleanup(self.connection.close)
        self.now = collector.utc_now()
        self.settings = dict(collector.DEFAULT_SETTINGS)
        self.channel = {"id": 1, "name": "Test", "username": "test", "entity": 1}
        self.connection.execute("INSERT INTO channels(channel_id, name) VALUES(1, 'Test')")
        self.connection.commit()

    def add_message(self, message_id, text):
        self.connection.execute(
            "INSERT INTO messages(channel_id, message_id, text, date_utc, channel_name) "
            "VALUES(?, ?, ?, ?, ?)",
            (1, message_id, text, collector.iso_utc(self.now - timedelta(minutes=1)), "Test"),
        )
        self.connection.commit()

    def collapse(self, first, second):
        base = {
            "channel_id": 1,
            "message_id": 1,
            "text": first,
            "date_utc": collector.iso_utc(self.now),
            "views": 1,
        }
        other = dict(base, message_id=2, text=second, views=2)
        return collector.collapse_duplicates([base, other], self.settings)

    def search(self, query, limit_override=None):
        return collector.search_database(
            self.connection,
            query,
            1,
            self.settings,
            [1],
            limit_override=limit_override,
        )

    def test_import_does_not_run_main(self):
        self.assertTrue(callable(collector.main))
        self.assertFalse((ROOT / "collector.lock").exists())

    def test_numbers_are_not_near_duplicates(self):
        prefix = "Подробная публикация о результатах проверки. " * 6
        kept, exact, near = self.collapse(prefix + "15 нарушений", prefix + "16 нарушений")
        self.assertEqual((len(kept), exact, near), (2, 0, 0))

    def test_negation_is_not_removed(self):
        prefix = "Подробная публикация о решении комиссии. " * 6
        kept, exact, near = self.collapse(prefix + "объект будет закрыт", prefix + "объект не будет закрыт")
        self.assertEqual((len(kept), exact, near), (2, 0, 0))

    def test_exact_duplicate_is_collapsed(self):
        text = "Одинаковая публикация с достаточно длинным текстом для проверки."
        kept, exact, near = self.collapse(text, text)
        self.assertEqual((len(kept), exact, near), (1, 1, 0))
        self.assertEqual(kept[0]["duplicates"][0]["message_id"], 2)

    def test_similar_but_distinct_text_is_preserved(self):
        kept, exact, near = self.collapse(
            "В городе открыли новую школу на улице Садовой.",
            "В городе открыли новую больницу на улице Садовой.",
        )
        self.assertEqual((len(kept), exact, near), (2, 0, 0))

    def test_eu_alias_is_searchable(self):
        self.add_message(1, "ЄС согласовал новое решение")
        self.assertEqual(len(self.search("ЕС")["direct_results"]), 1)

    def test_mobilization_does_not_match_mobile(self):
        self.add_message(1, "Объявлена мобилизация")
        self.add_message(2, "Новый мобильный телефон")
        results = self.search("мобилизация")["direct_results"]
        self.assertEqual([item["message_id"] for item in results], [1])

    def test_quoted_phrase_search(self):
        self.add_message(1, "Начались мирные переговоры сторон")
        self.add_message(2, "Переговоры продолжились, но мирные инициативы отложены")
        results = self.search('"мирные переговоры"')["direct_results"]
        self.assertEqual([item["message_id"] for item in results], [1])

    def test_search_result_limit(self):
        self.settings["search_max_results"] = 3
        for message_id in range(1, 6):
            self.add_message(message_id, "проверочная публикация")
        result = self.search("проверочная")
        self.assertEqual(result["total_direct_hits"], 5)
        self.assertEqual(len(result["direct_results"]), 3)
        self.assertTrue(result["truncated"])

    def test_local_search_needs_no_telegram_client(self):
        self.add_message(1, "Локальная проверка архива")
        result = self.search("архива")
        self.assertEqual(len(result["direct_results"]), 1)
        self.assertIsInstance(self.connection, sqlite3.Connection)


if __name__ == "__main__":
    unittest.main(verbosity=2)

