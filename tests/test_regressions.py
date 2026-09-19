import asyncio
import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "telegram_collector_free.py"
LOCK_PATH = ROOT / "collector.lock"
LOCK_STATE_BEFORE_IMPORT = (
    LOCK_PATH.exists(),
    LOCK_PATH.stat().st_mtime_ns if LOCK_PATH.exists() else None,
)
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
        collector.OUTPUT_DIR = self.directory / "Дайджесты"
        collector.ARCHIVE_DIR = collector.OUTPUT_DIR / "Архив"
        collector.RAW_DIR = collector.ARCHIVE_DIR / "Сырые"
        collector.LOG_DIR = self.directory / "logs"
        collector.LATEST_FILE = collector.OUTPUT_DIR / "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.json"
        collector.SEARCH_LATEST_FILE = collector.OUTPUT_DIR / "ПОИСК_ПОСЛЕДНИЙ.json"
        collector.SEARCH_ARCHIVE_DIR = collector.ARCHIVE_DIR / "Поиск"
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
        current_state = (
            LOCK_PATH.exists(),
            LOCK_PATH.stat().st_mtime_ns if LOCK_PATH.exists() else None,
        )
        self.assertEqual(current_state, LOCK_STATE_BEFORE_IMPORT)

    def test_telegram_code_prompt_strips_input(self):
        with patch("builtins.input", return_value=" 12345 ") as mocked_input:
            self.assertEqual(collector.prompt_telegram_code(), "12345")
        mocked_input.assert_called_once_with(
            "\nВведите код подтверждения из Telegram: "
        )


    def test_api_hash_validation_requires_32_hex_characters(self):
        self.assertTrue(collector.is_valid_api_hash("a" * 32))
        self.assertTrue(
            collector.is_valid_api_hash(
                "0123456789abcdef0123456789ABCDEF"
            )
        )
        self.assertFalse(collector.is_valid_api_hash("a" * 31))
        self.assertFalse(collector.is_valid_api_hash("\x16"))
        self.assertFalse(collector.is_valid_api_hash("g" * 32))

    def test_api_safety_defaults_are_conservative(self):
        self.assertGreaterEqual(
            collector.history_request_wait_seconds(self.settings),
            0.5,
        )
        self.assertEqual(
            collector.inter_channel_delay_seconds(self.settings, 50),
            0.75,
        )
        self.assertEqual(
            collector.inter_channel_delay_seconds(self.settings, 51),
            1.5,
        )
        self.assertGreaterEqual(
            collector.flood_wait_safety_seconds(self.settings),
            1,
        )

    def test_history_wait_time_cannot_be_disabled_accidentally(self):
        settings = dict(self.settings)
        settings["history_request_wait_seconds"] = 0
        self.assertEqual(
            collector.history_request_wait_seconds(settings),
            0.5,
        )

    def test_sync_channel_passes_explicit_wait_time_to_history_requests(self):
        class EmptyAsyncIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class FakeClient:
            def __init__(self):
                self.calls = []

            def iter_messages(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return EmptyAsyncIterator()

        client = FakeClient()
        result = asyncio.run(
            collector.sync_channel_once(
                client,
                self.connection,
                self.channel,
                24,
                self.settings,
            )
        )

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(client.calls), 2)
        expected = collector.history_request_wait_seconds(
            self.settings
        )
        for _, kwargs in client.calls:
            self.assertEqual(kwargs.get("wait_time"), expected)

    def test_sync_all_channels_stops_after_unhandled_flood_wait(self):
        channels = [
            dict(self.channel),
            {
                "id": 2,
                "name": "Second",
                "username": "second",
                "entity": 2,
            },
        ]
        failed = {
            "channel": "Test",
            "username": "test",
            "first_sync": False,
            "new": 0,
            "content_changed": 0,
            "metrics_changed": 0,
            "migrated": 0,
            "same": 0,
            "scanned": 0,
            "status": "error",
            "error": "FloodWait 120 сек.",
            "halt_sync": True,
        }

        with patch.object(
            collector,
            "sync_channel_with_retries",
            AsyncMock(return_value=failed),
        ) as mocked_sync:
            with patch.object(
                collector.asyncio,
                "sleep",
                AsyncMock(),
            ) as mocked_sleep:
                result = asyncio.run(
                    collector.sync_all_channels(
                        object(),
                        self.connection,
                        channels,
                        24,
                        self.settings,
                    )
                )

        self.assertEqual(mocked_sync.await_count, 1)
        mocked_sleep.assert_not_awaited()
        self.assertTrue(
            result["api_safety"]["halted_by_flood_wait"]
        )

    def test_history_backfill_is_skipped_after_sync_flood_stop(self):
        halted_sync = {
            "successful_channels": 0,
            "failed_channels": 1,
            "new_messages_saved": 0,
            "content_changed_messages_refreshed": 0,
            "metrics_changed_messages_refreshed": 0,
            "migrated_messages": 0,
            "unchanged_messages_refreshed": 0,
            "telegram_messages_scanned": 0,
            "channel_results": [],
            "api_safety": {
                "halted_by_flood_wait": True,
            },
        }

        with patch.object(
            collector,
            "sync_all_channels",
            AsyncMock(return_value=halted_sync),
        ):
            with patch.object(
                collector,
                "backfill_channel_history_with_retries",
                AsyncMock(),
            ) as mocked_backfill:
                status = asyncio.run(
                    collector._v4_ensure_history_for_search(
                        object(),
                        self.connection,
                        [self.channel],
                        30,
                        self.settings,
                    )
                )

        mocked_backfill.assert_not_awaited()
        self.assertFalse(status["complete"])
        self.assertEqual(status["incomplete_channels"], 1)

    def test_api_hash_prompt_retries_after_bad_hidden_paste(self):
        with patch.object(
            collector,
            "getpass",
            side_effect=["\x16", "a" * 32],
        ) as mocked_getpass:
            with patch("builtins.print") as mocked_print:
                self.assertEqual(
                    collector.prompt_api_hash(),
                    "a" * 32,
                )

        self.assertEqual(mocked_getpass.call_count, 2)
        printed = " ".join(
            str(argument)
            for call in mocked_print.call_args_list
            for argument in call.args
        )
        self.assertIn("длина 1", printed)
        self.assertIn("Shift+Insert", printed)

    def test_telegram_password_prompt_explains_hidden_input(self):
        with patch.object(
            collector,
            "getpass",
            return_value="секрет",
        ) as mocked_getpass:
            with patch("builtins.print") as mocked_print:
                self.assertEqual(
                    collector.prompt_telegram_password(),
                    "секрет",
                )

        mocked_getpass.assert_called_once_with(
            "Введите или вставьте пароль и нажмите Enter: "
        )
        printed = " ".join(
            str(argument)
            for call in mocked_print.call_args_list
            for argument in call.args
        )
        self.assertIn(
            "символы на экране не отображаются",
            printed,
        )

    def test_channel_number_range_selects_every_number(self):
        self.assertEqual(
            collector.parse_number_selection("30-33", 40),
            {30, 31, 32, 33},
        )

    def test_channel_number_range_rejects_out_of_bounds(self):
        with self.assertRaisesRegex(ValueError, "1-32"):
            collector.parse_number_selection("30-33", 32)

    def test_mixed_channel_selection_accepts_numbers_ranges_and_links(self):
        selected, public_values = collector.parse_mixed_channel_selection(
            "3,7-10,12-36,https://t.me/durov,@insiderUKR",
            40,
        )
        expected = {3, 7, 8, 9, 10} | set(range(12, 37))
        self.assertEqual(selected, expected)
        self.assertEqual(
            public_values,
            ["https://t.me/durov", "@insiderUKR"],
        )

    def test_mixed_channel_selection_accepts_all_plus_public_link(self):
        selected, public_values = collector.parse_mixed_channel_selection(
            "all,https://t.me/example_channel",
            3,
        )
        self.assertEqual(selected, {1, 2, 3})
        self.assertEqual(
            public_values,
            ["https://t.me/example_channel"],
        )

    def test_mixed_channel_selection_rejects_out_of_bounds_range(self):
        with self.assertRaisesRegex(ValueError, "1-35"):
            collector.parse_mixed_channel_selection(
                "3,12-36,https://t.me/durov",
                35,
            )

    def test_subscription_selection_resolves_link_from_same_input(self):
        subscribed = [
            SimpleNamespace(
                name="One",
                entity=SimpleNamespace(id=1, username="one"),
            ),
            SimpleNamespace(
                name="Two",
                entity=SimpleNamespace(id=2, username="two"),
            ),
            SimpleNamespace(
                name="Three",
                entity=SimpleNamespace(id=3, username="three"),
            ),
        ]
        public = {
            "id": 99,
            "name": "Public",
            "username": "public_channel",
            "entity": SimpleNamespace(id=99, username="public_channel"),
        }
        client = object()

        with patch(
            "builtins.input",
            return_value="1,3,https://t.me/public_channel",
        ):
            with patch.object(
                collector,
                "resolve_public_channel",
                AsyncMock(return_value=public),
            ) as mocked_resolve:
                with patch.object(
                    collector,
                    "prompt_add_public_channels",
                    AsyncMock(side_effect=lambda _client, items: items),
                ):
                    with patch.object(collector, "save_selection"):
                        result = asyncio.run(
                            collector.select_from_subscriptions(
                                client,
                                subscribed,
                            )
                        )

        self.assertEqual(
            [int(item["id"]) for item in result],
            [1, 3, 99],
        )
        mocked_resolve.assert_awaited_once_with(
            client,
            "https://t.me/public_channel",
        )

    def test_public_channel_prompt_does_not_reparse_number_range(self):
        existing = [
            {"id": 30, "name": "Thirty", "username": "thirty"},
            {"id": 31, "name": "Thirty One", "username": "thirty_one"},
        ]

        with patch("builtins.input", return_value="30-33"):
            with patch.object(
                collector,
                "resolve_public_channel",
            ) as mocked_resolve:
                with patch("builtins.print") as mocked_print:
                    result = asyncio.run(
                        collector.prompt_add_public_channels(
                            object(),
                            list(existing),
                        )
                    )

        self.assertEqual(result, existing)
        mocked_resolve.assert_not_awaited()
        printed = " ".join(
            str(argument)
            for call in mocked_print.call_args_list
            for argument in call.args
        )
        self.assertIn("номера из списка подписок", printed)
        self.assertIn("уже добавлены", printed)

    def test_readding_public_channel_repairs_stale_saved_username(self):
        existing = [
            {"id": 99, "name": "Pavel Durov", "username": None}
        ]
        fresh = {
            "id": 99,
            "name": "Pavel Durov",
            "username": "durov",
            "entity": SimpleNamespace(id=99, username="durov"),
        }

        with patch("builtins.input", return_value="https://t.me/durov"):
            with patch.object(
                collector,
                "resolve_public_channel",
                AsyncMock(return_value=fresh),
            ):
                with patch.object(collector, "save_selection") as mocked_save:
                    with patch("builtins.print") as mocked_print:
                        result = asyncio.run(
                            collector.prompt_add_public_channels(
                                object(),
                                list(existing),
                            )
                        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 99)
        self.assertEqual(result[0]["username"], "durov")
        mocked_save.assert_called_once()
        saved = mocked_save.call_args.args[0]
        self.assertEqual(saved[0]["username"], "durov")
        printed = " ".join(
            str(argument)
            for call in mocked_print.call_args_list
            for argument in call.args
        )
        self.assertIn("Обновлён: Pavel Durov (@durov)", printed)
        self.assertIn("Обновлено сохранённых каналов: 1", printed)

    def test_merge_resolved_channel_keeps_single_item_and_refreshes_name(self):
        existing = [{"id": 7, "name": "Old", "username": "channel"}]
        fresh = {
            "id": 7,
            "name": "New name",
            "username": "channel",
            "entity": object(),
        }
        result, added, updated = collector.merge_resolved_channel(
            list(existing),
            fresh,
        )
        self.assertFalse(added)
        self.assertTrue(updated)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "New name")

    def test_channel_add_does_not_fetch_all_subscriptions(self):
        existing = [{"id": 10, "name": "Saved", "username": "saved_channel"}]
        client = SimpleNamespace(get_dialogs=AsyncMock())

        with patch.object(collector, "load_selection", return_value=list(existing)):
            with patch.object(collector, "save_selection") as mocked_save:
                with patch.object(
                    collector,
                    "prompt_add_public_channels",
                    AsyncMock(return_value=list(existing)),
                ):
                    result = asyncio.run(
                        collector.resolve_channels(
                            client,
                            initial_action="a",
                            return_after_initial=True,
                        )
                    )

        self.assertEqual(result, existing)
        client.get_dialogs.assert_not_awaited()
        mocked_save.assert_called_once_with(existing)

    @unittest.skipUnless(collector.os.name == "nt", "Windows named mutex")
    def test_default_instance_lock_blocks_second_copy_across_folders(self):
        with collector.InstanceLock():
            original_app_dir = collector.APP_DIR
            try:
                collector.APP_DIR = self.directory / "other-copy"
                with self.assertRaisesRegex(RuntimeError, "уже запущен"):
                    with collector.InstanceLock():
                        pass
            finally:
                collector.APP_DIR = original_app_dir

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

    def test_runs_schema_has_selection_fingerprint(self):
        columns = collector.table_columns(self.connection, "runs")
        self.assertIn("selection_fingerprint", columns)

    def test_selection_fingerprint_is_order_independent(self):
        first = [
            {"id": 20, "name": "B"},
            {"id": 10, "name": "A"},
        ]
        second = [
            {"id": 10, "name": "Renamed"},
            {"id": 20, "name": "Other title"},
        ]
        self.assertEqual(
            collector.selection_fingerprint(first),
            collector.selection_fingerprint(second),
        )

    def test_selection_fingerprint_changes_with_channel_set(self):
        first = [{"id": 10}, {"id": 20}]
        second = [{"id": 10}, {"id": 30}]
        self.assertNotEqual(
            collector.selection_fingerprint(first),
            collector.selection_fingerprint(second),
        )

    def test_previous_digest_reference_is_scoped_to_exact_channel_set(self):
        channels_a = [{"id": 10}, {"id": 20}]
        channels_b = [{"id": 30}]
        fingerprint_a = collector.selection_fingerprint(channels_a)
        fingerprint_b = collector.selection_fingerprint(channels_b)

        rows = [
            ("2026-09-13T10:00:00+00:00", fingerprint_a),
            ("2026-09-13T11:00:00+00:00", fingerprint_b),
            ("2026-09-13T12:00:00+00:00", None),
        ]
        for created_utc, fingerprint in rows:
            self.connection.execute(
                """
                INSERT INTO runs (
                    created_utc, created_local, hours, messages_exported,
                    successful_channels, failed_channels, latest_file,
                    selection_fingerprint
                )
                VALUES (?, ?, 6, 1, 1, 0, 'digest.json', ?)
                """,
                (created_utc, created_utc, fingerprint),
            )
        self.connection.commit()

        self.assertEqual(
            collector.get_previous_digest_reference(self.connection, channels_a),
            ("2026-09-13T10:00:00+00:00", "news.db:runs"),
        )
        self.assertEqual(
            collector.get_previous_digest_reference(self.connection, channels_b),
            ("2026-09-13T11:00:00+00:00", "news.db:runs"),
        )
        self.assertEqual(
            collector.get_previous_digest_reference(
                self.connection,
                [{"id": 999}],
            ),
            (None, None),
        )

    def test_json_fallback_requires_matching_selection_fingerprint(self):
        channels = [{"id": 10}, {"id": 20}]
        fingerprint = collector.selection_fingerprint(channels)
        collector.ensure_dirs()

        collector.LATEST_FILE.write_text(
            __import__("json").dumps({
                "meta": {
                    "created_utc": "2026-09-13T10:30:00+00:00",
                    "selection_fingerprint": "wrong",
                }
            }),
            encoding="utf-8",
        )
        self.assertEqual(
            collector.get_previous_digest_reference(self.connection, channels),
            (None, None),
        )

        collector.LATEST_FILE.write_text(
            __import__("json").dumps({
                "meta": {
                    "created_utc": "2026-09-13T10:30:00+00:00",
                    "selection_fingerprint": fingerprint,
                }
            }),
            encoding="utf-8",
        )
        self.assertEqual(
            collector.get_previous_digest_reference(self.connection, channels),
            ("2026-09-13T10:30:00+00:00", "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.json"),
        )

    def test_register_run_and_export_store_same_selection_fingerprint(self):
        channels = [{"id": 10, "name": "A", "username": "a"}]
        fingerprint = collector.selection_fingerprint(channels)

        collector.register_run(
            self.connection,
            6,
            0,
            1,
            0,
            channels,
        )
        row = self.connection.execute(
            "SELECT selection_fingerprint FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["selection_fingerprint"], fingerprint)

        sync_stats = {
            "new_messages_saved": 0,
            "content_changed_messages_refreshed": 0,
            "metrics_changed_messages_refreshed": 0,
            "migrated_messages": 0,
            "telegram_messages_scanned": 0,
            "failed_channels": 0,
            "successful_channels": 1,
            "channel_results": [],
            "history_completeness": {"complete": True},
            "self_diagnostics": {},
        }
        result = collector._v4_save_output(
            [],
            [],
            [],
            0,
            0,
            6,
            channels,
            sync_stats,
            None,
            None,
            [],
            self.settings,
        )
        payload = __import__("json").loads(result[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["meta"]["selection_fingerprint"], fingerprint)
        self.assertFalse(payload["meta"]["comparison_available"])

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
        self.assertIn("telegram_url", request)
        self.assertIn("channel_url", request)
        self.assertIn("Markdown-ссылкой", request)
        self.assertIn("При наличии telegram_url", request)
        self.assertIn("иначе используй channel_url", request)
        self.assertIn("при отсутствии обеих ссылок", request)
        self.assertIn("URL не придумывай", request)
        self.assertIn("Каждый самостоятельный фактический сюжет должен завершаться строкой источника", request)
        self.assertIn("2–3 ключевые ссылки", request)

    def test_user_instructions_have_no_separate_post_link_or_color_markers(self):
        requests = (
            collector.EDITORIAL_PRINCIPLES
            + collector.SOURCE_RULES
            + collector.DIGEST_REQUEST
        )
        for phrase in (
            "Открыть публикацию",
            "Открыть пост",
            "Читать оригинал",
            "Перейти к сообщению",
        ):
            self.assertNotIn(phrase, requests)
        for marker in ("🔵", "🟢", "🟣", "🟠", "🟡", "🟤", "⚪", "🔴"):
            self.assertNotIn(marker, requests)

    def test_prompt_has_no_attachment_citation_hacks(self):
        self.assertFalse(hasattr(collector, "NO_ATTACHMENT_CITATIONS_RULE"))
        request = collector.DIGEST_REQUEST.lower()
        self.assertNotIn("file citations", request)
        self.assertNotIn("source chips", request)
        self.assertNotIn("citation вложения", request)
        self.assertNotIn("дайджест_последний", request)
        self.assertNotIn("поиск_последний", request)

    def test_digest_request_hides_technical_process(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("Служебные поля", request)
        self.assertIn("в готовом ответе", request)
        self.assertIn("Не обсуждай файл, JSON, локальную базу", request)
        self.assertIn("changes_since_previous_digest", request)
        self.assertIn("related_message_groups", request)
        self.assertIn("inherits_from_message_key", request)

    def test_digest_request_is_topic_neutral_and_automatic(self):
        request = collector.DIGEST_REQUEST.lower()
        for fixed_topic in ("харьков", "украина", "война"):
            self.assertNotIn(fixed_topic, request)
        self.assertIn("тематика заранее неизвестна", request)
        self.assertIn("без фиксированных рубрик", request)
        self.assertIn("по фактическому материалу", request)
        self.assertIn("заголовки делай короткими", request)

    def test_digest_request_limits_editorial_inference(self):
        request = collector.DIGEST_REQUEST.lower()
        self.assertIn("различай прямое сообщение", request)
        self.assertIn("независимое подтверждение", request)
        self.assertIn("редакционный вывод", request)
        self.assertIn("не достраивай отсутствующие факты", request)
        self.assertIn("автора действия, мотив, цель и причинность", request)
        self.assertIn("перепечатки одного исходного сообщения не считай независимыми подтверждениями", request)

    def test_digest_request_keeps_adaptive_compact_structure(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("«Главное за период»", request)
        self.assertIn("не ставь перед ним второй абзац с тем же резюме", request)
        self.assertIn("Однотипные оперативные предупреждения одного сюжета объединяй", request)
        self.assertIn("changes_since_previous_digest.comparison_available=true", request)
        self.assertIn("глубину определяй количеством реально новой информации", request)
        self.assertNotIn("при среднем объёме", request.lower())
        self.assertNotIn("при большом", request.lower())

    def test_digest_comparison_never_replaces_full_period(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("Основной дайджест всегда строй по всему содержательному материалу", request)
        self.assertIn("changes_since_previous_digest — только дополнительный слой сравнения", request)
        self.assertIn("не задаёт временные границы основного дайджеста", request)
        self.assertIn("не является фильтром отбора", request)
        self.assertIn("не исключай из основного дайджеста", request)

    def test_digest_hides_internal_coverage_and_comparison_rules(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("Не объясняй читателю внутренние правила охвата и сравнения", request)
        self.assertIn("молча применяй полный период основного выпуска", request)
        self.assertIn("не комментируя их в готовом тексте", request)
        self.assertEqual(request.count("Не объясняй читателю внутренние правила охвата и сравнения"), 1)
        self.assertIn("всего охваченного материала по date_local", request)
        self.assertIn("а не только сообщений из блока сравнения", request)
        self.assertIn("«Что изменилось» не заменяет основной дайджест", request)
        self.assertEqual(request.count("changes_since_previous_digest — только дополнительный слой сравнения"), 1)
        self.assertEqual(request.count("«Что изменилось» не заменяет основной дайджест"), 1)

    def test_digest_does_not_end_with_subjective_second_summary(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("не добавляй повторный итог, личный выбор или рейтинг", request)
        self.assertIn("«Что изменилось» не заменяет основной дайджест", request)
        self.assertNotIn("что я бы выделил", request.lower())
        self.assertNotIn("мой выбор", request.lower())

    def test_digest_title_uses_actual_local_period(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("В заголовке укажи дату", request)
        self.assertIn("фактический локальный интервал", request)
        self.assertIn("date_local", request)
        self.assertIn("если надёжно определить интервал нельзя, не придумывай", request)

    def test_source_rules_cover_independent_and_composite_stories(self):
        rules = collector.SOURCE_RULES
        self.assertIn("Каждый самостоятельный фактический сюжет должен завершаться строкой источника", rules)
        self.assertIn("Строка источника должна быть последней строкой сюжета", rules)
        self.assertIn("покрывать все существенные утверждения", rules)
        self.assertIn("иначе раздели материал на отдельные сюжеты или пункты", rules)
        self.assertIn("ставь источник непосредственно после каждого события", rules)
        self.assertIn("не собирай общий список ссылок в конце блока", rules)
        self.assertIn("Пункты «Главное за период» могут не дублировать ссылки", rules)
        self.assertIn("2–3 ключевые ссылки", rules)
        self.assertEqual(rules.count("Каждый самостоятельный фактический сюжет"), 1)
        self.assertEqual(rules.count("Строка источника должна быть последней строкой сюжета"), 1)
        self.assertNotIn("не переходи к следующему заголовку или самостоятельному сюжету", rules)
        self.assertNotIn("Источник ставь после соответствующего сюжета или пункта", rules)

    def test_digest_isolated_from_user_profile_and_chat_history(self):
        rules = collector.EDITORIAL_PRINCIPLES
        self.assertIn("игнорируй сведения о пользователе", rules)
        self.assertIn("персональную память", rules)
        self.assertIn("историю текущего и прошлых чатов", rules)
        self.assertIn("не должны влиять на отбор, порядок, акценты или оценку полезности материала", rules)
        self.assertIn("Не пиши «для вас», «вам особенно важно»", rules)
        self.assertEqual(rules.count("игнорируй сведения о пользователе"), 1)
        self.assertEqual(rules.count("Не пиши «для вас»"), 1)

    def test_editorial_rules_do_not_force_analysis_after_every_story(self):
        rules = collector.EDITORIAL_PRINCIPLES
        self.assertIn("если факты самодостаточны", rules)
        self.assertIn("не дописывай обязательную аналитику", rules)
        self.assertIn("не ранжируй событие", rules.lower())

    def test_prompt_size_budget(self):
        self.assertLess(len(collector.EDITORIAL_PRINCIPLES), 3500)
        self.assertLess(len(collector.SOURCE_RULES), 1200)
        self.assertLess(len(collector.DIGEST_REQUEST), 6000)

    def test_digest_profile_version_is_an_independent_export_contract(self):
        self.assertEqual(collector.EXPORT_SCHEMA_VERSION, 6)
        self.assertEqual(collector.DIGEST_PROFILE_VERSION, "6.0")
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"schema_version": EXPORT_SCHEMA_VERSION', source)
        self.assertIn('"digest_profile_version": DIGEST_PROFILE_VERSION', source)

    def test_export_contract_is_ai_vendor_neutral(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"recommended_ai_request"', source)
        self.assertNotIn('"recommended_chatgpt_request"', source)
        self.assertTrue(hasattr(collector, "build_search_ai_instruction"))
        self.assertFalse(hasattr(collector, "build_search_chatgpt_instruction"))
        self.assertNotIn("ЗАГРУЗИТЬ_В_CHATGPT_", source)

    def test_readme_positions_json_as_portable_ai_input(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("структурированных JSON-выгрузок для анализа в ИИ-ассистентах", readme)
        self.assertIn("Формат выгрузки не привязан к конкретной модели", readme)
        self.assertIn("основное тестирование TelegramNewsAI проводится в ChatGPT", readme)
        self.assertIn("может различаться между платформами", readme)
        self.assertNotIn("подготовки JSON для анализа в ChatGPT", readme)

    def test_user_facing_source_documentation_matches_fallback_order(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("источник по возможности ведёт непосредственно на конкретную исходную публикацию", readme)
        self.assertIn("ссылка на сам канал используется только как резервный вариант", readme)
        self.assertIn("не добавляет отдельную ссылку «Открыть публикацию»", readme)

    def test_maintenance_text_has_no_obsolete_release_labels(self):
        source = SOURCE.read_text(encoding="utf-8")
        for obsolete in (
            "TelegramNewsAI 5.4 —",
            "Поиск 5.4",
            "Speed profile 5.1",
            "Базовый поиск 5.4",
            "обычный поиск 5.4",
        ):
            self.assertNotIn(obsolete, source)

    def test_installer_requires_supported_python(self):
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("$minimumPython = [Version]'3.10'", installer)
        self.assertIn("Get-PythonVersion", installer)
        self.assertIn("$venvDir.unsupported-", installer)

    def test_release_builder_uses_exact_maintenance_distribution(self):
        builder = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
        for required in ("'LICENSE'", "'SECURITY.md'", "'Telegram_Digest.exe.sha256'"):
            self.assertIn(required, builder)
        self.assertNotIn("'.gitignore'", builder)

    def test_ci_verifies_and_uploads_the_release_artifact(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("public-readiness-final", workflow)
        self.assertIn("actions/checkout@v7", workflow)
        self.assertIn("actions/setup-python@v7", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("Verify exact release ZIP contents", workflow)

    def test_preview_has_no_fixed_topic_classifier(self):
        self.assertNotIn("topicRules", collector.PREVIEW_HTML)
        self.assertNotIn('id="topic"', collector.PREVIEW_HTML)
        self.assertIn("m.channel_url", collector.PREVIEW_HTML)

    def test_preview_uses_one_source_link_with_post_priority(self):
        preview = collector.PREVIEW_HTML
        self.assertIn("m.telegram_url||m.channel_url", preview)
        self.assertNotIn("Открыть публикацию", preview)

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
        request = collector.build_search_ai_instruction(
            "проверочная тема",
            7,
            {},
        )
        self.assertIn("telegram_url", request)
        self.assertIn("channel_url", request)
        self.assertIn("иначе используй channel_url", request)
        self.assertIn("URL не придумывай", request)
        self.assertIn("related_message_groups", request)
        self.assertIn("по этой теме, а не по всей повестке", request)
        self.assertIn("редакционный вывод", request)
        self.assertNotIn("Открыть публикацию", request)
        self.assertNotIn("file citations", request.lower())
        self.assertNotIn("source chips", request.lower())
        self.assertLess(len(request), 5500)

    def test_editorial_requests_do_not_depend_on_chatgpt_ui(self):
        requests = collector.DIGEST_REQUEST + collector.build_search_ai_instruction(
            "проверочная тема", 7, {}
        )
        self.assertNotIn("ChatGPT", requests)
        self.assertNotIn("file citations", requests.lower())
        self.assertNotIn("source chips", requests.lower())

    def test_generated_digest_json_is_portable_and_uses_new_contract(self):
        channels = [{"id": 10, "name": "A", "username": "a"}]
        sync_stats = {
            "new_messages_saved": 0,
            "content_changed_messages_refreshed": 0,
            "metrics_changed_messages_refreshed": 0,
            "migrated_messages": 0,
            "telegram_messages_scanned": 0,
            "failed_channels": 0,
            "successful_channels": 1,
            "channel_results": [],
            "history_completeness": {"complete": True},
            "self_diagnostics": {},
        }
        latest, archive, _, _ = collector._v4_save_output(
            [], [], [], 0, 0, 6, channels, sync_stats, None, None, [], self.settings
        )
        payload = __import__("json").loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(payload["meta"]["schema_version"], 6)
        self.assertEqual(payload["meta"]["digest_profile_version"], "6.0")
        self.assertIn("recommended_ai_request", payload["meta"])
        self.assertNotIn("recommended_chatgpt_request", payload["meta"])
        self.assertTrue(archive.name.startswith("ДАЙДЖЕСТ_"))


class MenuCancellationRegressionTests(unittest.TestCase):
    def test_back_command_aliases(self):
        for value in ("0", "назад", "НАЗАД", "отмена", "back"):
            with self.subTest(value=value):
                self.assertTrue(collector.is_back_command(value))
        self.assertFalse(collector.is_back_command(""))

    def test_add_channels_can_be_cancelled_without_changes(self):
        original = [{"id": 1, "name": "Test", "username": "test"}]
        with patch("builtins.input", return_value="0"), patch("builtins.print"):
            result = asyncio.run(
                collector.prompt_add_public_channels(object(), original.copy())
            )
        self.assertEqual(result, original)

    def test_remove_channels_can_be_cancelled_without_changes(self):
        original = [{"id": 1, "name": "Test", "username": "test"}]
        with patch("builtins.input", return_value="назад"), patch("builtins.print"):
            result = collector.prompt_remove_channels(original.copy())
        self.assertEqual(result, original)

    def test_recreate_channel_list_can_be_cancelled(self):
        with patch("builtins.input", return_value="0"), patch("builtins.print"):
            result = asyncio.run(
                collector.select_from_subscriptions(object(), [])
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
