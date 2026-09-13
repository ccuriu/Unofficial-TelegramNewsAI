import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace


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

    def test_public_link_uses_username_and_message_id(self):
        self.assertEqual(
            collector.public_link("example", 12345),
            "https://t.me/example/12345",
        )

    def test_public_link_without_username_is_none(self):
        self.assertIsNone(collector.public_link(None, 12345))

    def test_public_channel_link_uses_username(self):
        self.assertEqual(
            collector.public_channel_link("example"),
            "https://t.me/example",
        )

    def test_public_channel_link_without_username_is_none(self):
        self.assertIsNone(collector.public_channel_link(None))

    def test_invalid_public_username_is_not_linked(self):
        self.assertIsNone(collector.public_channel_link("not valid"))

    def test_normalized_message_export_keeps_telegram_url(self):
        message = collector.make_message(
            self.channel,
            SimpleNamespace(
                id=12345,
                message="Сообщение для проверки ссылки",
                date=self.now,
            ),
            self.settings,
        )
        self.assertEqual(message["channel_url"], "https://t.me/test")
        self.assertEqual(message["telegram_url"], "https://t.me/test/12345")

    def test_compact_exact_duplicate_keeps_telegram_url(self):
        text = "Одинаковая публикация с достаточно длинным текстом для проверки."
        first = {
            "channel_id": 1,
            "channel": "Канал A",
            "username": "channel_a",
            "message_id": 1,
            "channel_url": "https://t.me/channel_a",
            "telegram_url": "https://t.me/channel_a/1",
            "text": text,
            "date_utc": collector.iso_utc(self.now),
            "views": 1,
        }
        second = dict(
            first,
            channel_id=2,
            channel="Канал B",
            username="channel_b",
            message_id=2,
            channel_url="https://t.me/channel_b",
            telegram_url="https://t.me/channel_b/2",
            views=2,
        )
        kept, exact, near = collector.collapse_duplicates(
            [first, second], self.settings
        )
        self.assertEqual((len(kept), exact, near), (1, 1, 0))
        self.assertEqual(
            kept[0]["duplicates"][0]["channel_url"],
            "https://t.me/channel_b",
        )
        self.assertEqual(
            kept[0]["duplicates"][0]["telegram_url"],
            "https://t.me/channel_b/2",
        )

    def test_related_group_member_keeps_telegram_url(self):
        common_url = "https://example.test/source"
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "username": "channel_a",
                "message_id": 10,
                "channel_url": "https://t.me/channel_a",
                "telegram_url": "https://t.me/channel_a/10",
                "canonical_urls": [common_url],
            },
            {
                "channel_id": 2,
                "channel": "Канал B",
                "username": "channel_b",
                "message_id": 20,
                "channel_url": "https://t.me/channel_b",
                "telegram_url": "https://t.me/channel_b/20",
                "canonical_urls": [common_url],
            },
        ]
        groups = collector.build_related_groups(messages)
        self.assertEqual(
            [item["channel_url"] for item in groups[0]["message_refs"]],
            ["https://t.me/channel_a", "https://t.me/channel_b"],
        )
        self.assertEqual(
            [item["telegram_url"] for item in groups[0]["message_refs"]],
            ["https://t.me/channel_a/10", "https://t.me/channel_b/20"],
        )

    def test_digest_request_uses_telegram_urls_for_sources(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("channel_url", request)
        self.assertIn("telegram_url", request)
        self.assertIn("Markdown-ссылками", request)
        self.assertIn("1–3 наиболее полезных источников", request)

    def test_digest_request_hides_internal_references(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("refs", request)
        self.assertIn("message_key", request)
        self.assertIn("Никогда не показывай их пользователю", request)
        self.assertIn("Никогда не ссылайся на исходный JSON-файл", request)

    def test_digest_request_is_topic_neutral_and_automatic(self):
        request = collector.DIGEST_REQUEST.lower()
        for fixed_topic in ("харьков", "украина", "война"):
            self.assertNotIn(fixed_topic, request)
        self.assertIn("определи характер материала", request)
        self.assertIn("самостоятельно создай естественные темы", request)
        self.assertIn("адаптируй стиль анализа", request)
        self.assertIn("фиксированного набора разделов нет", request)
        self.assertIn("цветные unicode/emoji-маркеры", request)

    def test_preview_has_no_fixed_topic_classifier(self):
        self.assertNotIn("topicRules", collector.PREVIEW_HTML)
        self.assertNotIn('id="topic"', collector.PREVIEW_HTML)
        self.assertIn("m.channel_url", collector.PREVIEW_HTML)

    def test_legacy_operational_hook_has_no_geographic_bias(self):
        self.assertFalse(collector.is_routine_alert("Короткое сообщение любого содержания"))

    def test_exports_do_not_assign_fixed_topics(self):
        samples = {
            "news": "Городской совет утвердил новый график движения",
            "screen": "Студия объявила дату премьеры сериала",
            "science": "Исследователи опубликовали результаты эксперимента",
            "technology": "Компания представила новую вычислительную платформу",
            "mixed": "Материал объединяет культурное событие и деловую встречу",
        }
        for label, text in samples.items():
            with self.subTest(label=label):
                message = collector.make_message(
                    self.channel,
                    SimpleNamespace(id=100, message=text, date=self.now),
                    self.settings,
                )
                self.assertNotIn("topic", message)
                self.assertNotIn("category", message)

    def test_topic_search_request_uses_human_sources(self):
        request = collector.build_search_chatgpt_instruction(
            "проверочная тема",
            7,
            {},
        )
        self.assertIn("channel_url", request)
        self.assertIn("telegram_url", request)
        self.assertIn("Открыть публикацию", request)
        self.assertIn("не придумывай", request)
        self.assertIn("refs", request)
        self.assertIn("Не используй фиксированный набор разделов", request)
        self.assertIn("Никогда не ссылайся на исходный JSON-файл", request)


if __name__ == "__main__":
    unittest.main(verbosity=2)

