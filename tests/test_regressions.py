import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


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
        collector.SETTINGS_FILE = self.directory / "settings_free.json"
        collector.OUTPUT_DIR = self.directory / "Дайджесты"
        collector.ARCHIVE_DIR = collector.OUTPUT_DIR / "Архив"
        collector.RAW_DIR = collector.ARCHIVE_DIR / "Сырые"
        collector.LOG_DIR = self.directory / "logs"
        collector.LATEST_FILE = collector.OUTPUT_DIR / "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.json"
        collector.AI_LATEST_FILE = collector.OUTPUT_DIR / "ДАЙДЖЕСТ_ДЛЯ_ИИ.md"
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
            1.0,
        )
        self.assertEqual(collector.MAX_SELECTED_CHANNELS, 50)
        self.assertNotIn("bulk_channel_threshold", collector.DEFAULT_SETTINGS)
        self.assertNotIn(
            "bulk_inter_channel_delay_seconds",
            collector.DEFAULT_SETTINGS,
        )
        self.assertEqual(
            collector.DEFAULT_SETTINGS["telethon_flood_sleep_threshold_seconds"],
            0,
        )
        self.assertEqual(
            collector.DEFAULT_SETTINGS["max_flood_wait_seconds"],
            0,
        )
        self.assertTrue(
            collector.DEFAULT_SETTINGS["stop_on_any_flood_wait"]
        )

    def test_load_settings_migrates_exact_old_standard_pacing_only(self):
        old = dict(collector.DEFAULT_SETTINGS)
        old.update({
            "history_request_wait_seconds": 0.5,
            "inter_channel_delay_seconds": 0.25,
            "telethon_flood_sleep_threshold_seconds": 0,
            "max_flood_wait_seconds": 0,
            "stop_on_any_flood_wait": True,
            "refresh_recent_messages": 50,
            "refresh_recent_hours": 2,
        })
        collector.SETTINGS_FILE.write_text(
            json.dumps(old),
            encoding="utf-8",
        )

        loaded = collector.load_settings()

        self.assertEqual(loaded["inter_channel_delay_seconds"], 1.0)
        persisted = json.loads(
            collector.SETTINGS_FILE.read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["inter_channel_delay_seconds"], 1.0)

    def test_load_settings_preserves_manual_inter_channel_delay(self):
        custom = dict(collector.DEFAULT_SETTINGS)
        custom["inter_channel_delay_seconds"] = 0.6
        collector.SETTINGS_FILE.write_text(
            json.dumps(custom),
            encoding="utf-8",
        )

        loaded = collector.load_settings()

        self.assertEqual(loaded["inter_channel_delay_seconds"], 0.6)

    def test_sync_all_channels_rejects_more_than_product_limit_before_network(self):
        channels = [
            {
                "id": index,
                "name": f"Channel {index}",
                "username": f"channel_{index}",
                "entity": index,
            }
            for index in range(1, collector.MAX_SELECTED_CHANNELS + 2)
        ]
        with patch.object(
            collector,
            "sync_channel_with_retries",
            AsyncMock(),
        ) as mocked_sync:
            with self.assertRaisesRegex(ValueError, "максимум 50"):
                asyncio.run(
                    collector.sync_all_channels(
                        object(),
                        self.connection,
                        channels,
                        24,
                        self.settings,
                    )
                )
        mocked_sync.assert_not_awaited()

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
        # Первый sync сохраняет отдельный refresh ради правок/метрик,
        # которые могли измениться во время первоначального backfill.
        self.assertEqual(len(client.calls), 2)
        expected = collector.history_request_wait_seconds(
            self.settings
        )
        for _, kwargs in client.calls:
            self.assertEqual(kwargs.get("wait_time"), expected)

    def test_incremental_sync_skips_refresh_without_recent_known_message(self):
        old_date = self.now - timedelta(hours=3)
        old_item = collector.make_message(
            self.channel,
            SimpleNamespace(
                id=10,
                message="Старое сообщение",
                date=old_date,
                views=1,
            ),
            self.settings,
        )
        collector.upsert_message(self.connection, old_item)
        collector.upsert_channel_state(
            self.connection,
            self.channel,
            last_message_id=10,
            success=True,
        )
        self.connection.commit()

        class ListAsyncIterator:
            def __init__(self, items):
                self.items = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeClient:
            def __init__(self):
                self.calls = []

            def iter_messages(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return ListAsyncIterator([
                    SimpleNamespace(
                        id=11,
                        message="Новое сообщение",
                        date=self_now,
                        views=2,
                    )
                ])

        self_now = self.now
        client = FakeClient()
        telemetry = collector.new_api_safety_telemetry(1)

        result = asyncio.run(
            collector.sync_channel_once(
                client,
                self.connection,
                self.channel,
                24,
                self.settings,
                safety_telemetry=telemetry,
            )
        )

        self.assertEqual(result["new"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(telemetry["history_iterators_started"], 1)
        state = collector.get_channel_state(self.connection, 1)
        self.assertEqual(state["last_message_id"], 11)

    def test_refresh_keeps_edits_metrics_and_does_not_hide_late_message(self):
        original = collector.make_message(
            self.channel,
            SimpleNamespace(
                id=10,
                message="Исходный текст",
                date=self.now - timedelta(minutes=30),
                views=10,
            ),
            self.settings,
        )
        collector.upsert_message(self.connection, original)
        collector.upsert_channel_state(
            self.connection,
            self.channel,
            last_message_id=10,
            success=True,
        )
        self.connection.commit()

        new_message = SimpleNamespace(
            id=11,
            message="Новое сообщение",
            date=self.now,
            views=1,
        )
        late_message = SimpleNamespace(
            id=12,
            message="Появилось во время refresh",
            date=self.now,
            views=1,
        )
        refreshed_new = SimpleNamespace(
            id=11,
            message="Новое сообщение",
            date=self.now,
            views=2,
        )
        edited_old = SimpleNamespace(
            id=10,
            message="Исправленный текст",
            date=self.now - timedelta(minutes=30),
            views=25,
        )

        class ListAsyncIterator:
            def __init__(self, items):
                self.items = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeClient:
            def __init__(self):
                self.calls = []
                self.batches = [
                    [new_message],
                    [late_message, refreshed_new, edited_old],
                ]

            def iter_messages(self, *args, **kwargs):
                index = len(self.calls)
                self.calls.append((args, kwargs))
                return ListAsyncIterator(self.batches[index])

        client = FakeClient()
        telemetry = collector.new_api_safety_telemetry(1)
        result = asyncio.run(
            collector.sync_channel_once(
                client,
                self.connection,
                self.channel,
                24,
                self.settings,
                safety_telemetry=telemetry,
            )
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(telemetry["history_iterators_started"], 2)
        self.assertGreaterEqual(result["content_changed"], 1)
        self.assertGreaterEqual(result["metrics_changed"], 1)
        state = collector.get_channel_state(self.connection, 1)
        self.assertEqual(state["last_message_id"], 11)
        late = self.connection.execute(
            "SELECT 1 FROM messages WHERE channel_id = 1 AND message_id = 12"
        ).fetchone()
        self.assertIsNone(late)

    def test_transient_retry_is_counted_but_safety_errors_are_not_retried(self):
        client = SimpleNamespace(
            is_connected=Mock(return_value=True),
            connect=AsyncMock(),
        )
        telemetry = collector.new_api_safety_telemetry(1)
        ok = collector.init_channel_sync_stats(self.channel, False)

        with patch.object(
            collector,
            "sync_channel_once",
            AsyncMock(side_effect=[RuntimeError("temporary"), ok]),
        ) as mocked_sync:
            with patch.object(
                collector.asyncio,
                "sleep",
                AsyncMock(),
            ) as mocked_sleep:
                result = asyncio.run(
                    collector.sync_channel_with_retries(
                        client,
                        self.connection,
                        self.channel,
                        24,
                        self.settings,
                        safety_telemetry=telemetry,
                    )
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(mocked_sync.await_count, 2)
        mocked_sleep.assert_awaited_once()
        self.assertEqual(telemetry["network_retry_count"], 1)

    def test_sync_channel_with_retries_does_not_retry_flood_wait(self):
        class SyntheticFloodWaitError(Exception):
            def __init__(self, seconds):
                super().__init__(f"FloodWait {seconds}")
                self.seconds = seconds

        client = SimpleNamespace(
            is_connected=Mock(return_value=True),
            connect=AsyncMock(),
        )
        error = SyntheticFloodWaitError(120)

        with patch.object(
            collector,
            "FloodWaitError",
            SyntheticFloodWaitError,
        ):
            with patch.object(
                collector,
                "sync_channel_once",
                AsyncMock(side_effect=error),
            ) as mocked_sync:
                with patch.object(
                    collector.asyncio,
                    "sleep",
                    AsyncMock(),
                ) as mocked_sleep:
                    result = asyncio.run(
                        collector.sync_channel_with_retries(
                            client,
                            self.connection,
                            self.channel,
                            24,
                            self.settings,
                        )
                    )

        self.assertEqual(mocked_sync.await_count, 1)
        mocked_sleep.assert_not_awaited()
        self.assertTrue(result["halt_sync"])
        self.assertEqual(result["halt_reason"], "flood_wait")
        self.assertIn("FloodWait 120 сек.", result["error"])

    def test_sync_channel_with_retries_does_not_retry_restriction_errors(self):
        client = SimpleNamespace(
            is_connected=Mock(return_value=True),
            connect=AsyncMock(),
        )
        SessionRevokedError = type(
            "SessionRevokedError",
            (Exception,),
            {},
        )

        for label, error in (
            ("peer_flood", RuntimeError("PEER_FLOOD")),
            ("session_revoked", SessionRevokedError("revoked")),
            ("frozen_account", RuntimeError("FROZEN_METHOD_INVALID")),
        ):
            with self.subTest(signal=label):
                with patch.object(
                    collector,
                    "sync_channel_once",
                    AsyncMock(side_effect=error),
                ) as mocked_sync:
                    with patch.object(
                        collector.asyncio,
                        "sleep",
                        AsyncMock(),
                    ) as mocked_sleep:
                        result = asyncio.run(
                            collector.sync_channel_with_retries(
                                client,
                                self.connection,
                                self.channel,
                                24,
                                self.settings,
                            )
                        )

                self.assertEqual(mocked_sync.await_count, 1)
                mocked_sleep.assert_not_awaited()
                self.assertTrue(result["halt_sync"])
                self.assertEqual(
                    result["halt_reason"],
                    "account_or_api_restriction",
                )

    def test_backfill_does_not_retry_flood_wait(self):
        class SyntheticFloodWaitError(Exception):
            def __init__(self, seconds):
                super().__init__(f"FloodWait {seconds}")
                self.seconds = seconds

        error = SyntheticFloodWaitError(90)

        with patch.object(
            collector,
            "FloodWaitError",
            SyntheticFloodWaitError,
        ):
            with patch.object(
                collector,
                "backfill_channel_history_once",
                AsyncMock(side_effect=error),
            ) as mocked_backfill:
                with patch.object(
                    collector.asyncio,
                    "sleep",
                    AsyncMock(),
                ) as mocked_sleep:
                    result = asyncio.run(
                        collector.backfill_channel_history_with_retries(
                            object(),
                            self.connection,
                            self.channel,
                            self.now - timedelta(days=30),
                            self.settings,
                        )
                    )

        self.assertEqual(mocked_backfill.await_count, 1)
        mocked_sleep.assert_not_awaited()
        self.assertTrue(result["halt_sync"])
        self.assertEqual(result["halt_reason"], "flood_wait")

    def test_backfill_does_not_retry_peer_flood(self):
        with patch.object(
            collector,
            "backfill_channel_history_once",
            AsyncMock(side_effect=RuntimeError("PEER_FLOOD")),
        ) as mocked_backfill:
            with patch.object(
                collector.asyncio,
                "sleep",
                AsyncMock(),
            ) as mocked_sleep:
                result = asyncio.run(
                    collector.backfill_channel_history_with_retries(
                        object(),
                        self.connection,
                        self.channel,
                        self.now - timedelta(days=30),
                        self.settings,
                    )
                )

        self.assertEqual(mocked_backfill.await_count, 1)
        mocked_sleep.assert_not_awaited()
        self.assertTrue(result["halt_sync"])
        self.assertEqual(
            result["halt_reason"],
            "account_or_api_restriction",
        )

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
            "halt_reason": "flood_wait",
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
        self.assertTrue(
            result["api_safety"]["halted_by_api_safety"]
        )
        self.assertEqual(
            result["api_safety"]["halt_reason"],
            "flood_wait",
        )

    def test_api_safety_summary_is_logged_and_contains_no_secrets(self):
        ok = collector.init_channel_sync_stats(self.channel, False)
        ok["scanned"] = 3

        with patch.object(
            collector,
            "sync_channel_with_retries",
            AsyncMock(return_value=ok),
        ):
            with patch.object(
                collector,
                "log_info",
            ) as mocked_log:
                result = asyncio.run(
                    collector.sync_all_channels(
                        object(),
                        self.connection,
                        [self.channel],
                        24,
                        self.settings,
                    )
                )

        summary = result["api_safety_summary"]
        self.assertTrue(summary.startswith("API_SAFETY_SUMMARY | "))
        self.assertIn("channel_count=1", summary)
        self.assertIn("channels_completed=1", summary)
        self.assertIn("messages_scanned=3", summary)
        self.assertIn("history_iterators_started=0", summary)
        self.assertIn("network_retries=0", summary)
        lowered = summary.casefold()
        for secret_name in (
            "api_hash",
            "auth_key",
            "phone_code",
            "session_contents",
        ):
            self.assertNotIn(secret_name, lowered)
        self.assertTrue(
            any(
                call.args
                and str(call.args[0]).startswith("API_SAFETY_SUMMARY | ")
                for call in mocked_log.call_args_list
            )
        )

    def test_flood_wait_seconds_are_recorded_in_safety_telemetry(self):
        class SyntheticFloodWaitError(Exception):
            def __init__(self, seconds):
                super().__init__(f"FloodWait {seconds}")
                self.seconds = seconds

        client = SimpleNamespace(
            is_connected=Mock(return_value=True),
            connect=AsyncMock(),
        )
        telemetry = collector.new_api_safety_telemetry(1)

        with patch.object(
            collector,
            "FloodWaitError",
            SyntheticFloodWaitError,
        ):
            with patch.object(
                collector,
                "sync_channel_once",
                AsyncMock(side_effect=SyntheticFloodWaitError(77)),
            ):
                result = asyncio.run(
                    collector.sync_channel_with_retries(
                        client,
                        self.connection,
                        self.channel,
                        24,
                        self.settings,
                        safety_telemetry=telemetry,
                    )
                )

        self.assertTrue(result["halt_sync"])
        self.assertTrue(telemetry["flood_wait"])
        self.assertEqual(telemetry["flood_wait_seconds"], 77)
        self.assertTrue(telemetry["safety_halt"])
        self.assertEqual(telemetry["halt_reason"], "flood_wait")

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

    def test_public_channel_prompt_stops_at_product_limit_without_network(self):
        existing = [
            {
                "id": index,
                "name": f"Channel {index}",
                "username": f"channel_{index}",
            }
            for index in range(1, collector.MAX_SELECTED_CHANNELS + 1)
        ]
        with patch.object(
            collector,
            "resolve_public_channel",
            AsyncMock(),
        ) as mocked_resolve:
            result = asyncio.run(
                collector.prompt_add_public_channels(
                    object(),
                    existing,
                )
            )
        self.assertEqual(len(result), collector.MAX_SELECTED_CHANNELS)
        mocked_resolve.assert_not_awaited()

    def test_saved_selection_over_limit_is_blocked_before_telegram_restore(self):
        saved = [
            {
                "id": index,
                "name": f"Channel {index}",
                "username": f"channel_{index}",
            }
            for index in range(1, collector.MAX_SELECTED_CHANNELS + 2)
        ]
        with patch.object(
            collector,
            "load_selection",
            return_value=saved,
        ):
            with patch.object(
                collector,
                "restore_saved_selection_from_session",
                AsyncMock(),
            ) as mocked_restore:
                result = asyncio.run(
                    collector.resolve_channels(
                        object(),
                        skip_menu=True,
                    )
                )
        self.assertEqual(result, [])
        mocked_restore.assert_not_awaited()

    def test_subscription_selection_rejects_51_and_accepts_50(self):
        subscribed = [
            SimpleNamespace(
                name=f"Channel {index}",
                entity=SimpleNamespace(
                    id=index,
                    username=f"channel_{index}",
                ),
            )
            for index in range(1, 52)
        ]
        with patch(
            "builtins.input",
            side_effect=["all", "1-50"],
        ):
            with patch.object(
                collector,
                "prompt_add_public_channels",
                AsyncMock(side_effect=lambda _client, items: items),
            ):
                with patch.object(collector, "save_selection"):
                    result = asyncio.run(
                        collector.select_from_subscriptions(
                            object(),
                            subscribed,
                        )
                    )
        self.assertEqual(len(result), 50)
        self.assertEqual(
            {int(item["id"]) for item in result},
            set(range(1, 51)),
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

    def test_normal_digest_restores_saved_channels_from_session_cache(self):
        existing = [
            {
                "id": 10,
                "name": "Saved",
                "username": "saved_channel",
            }
        ]
        input_entity = SimpleNamespace(channel_id=10)
        session = SimpleNamespace(
            get_input_entity=Mock(return_value=input_entity),
        )
        client = SimpleNamespace(
            session=session,
            get_dialogs=AsyncMock(),
        )

        with patch.object(
            collector,
            "load_selection",
            return_value=list(existing),
        ):
            result = asyncio.run(
                collector.resolve_channels(
                    client,
                    skip_menu=True,
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 10)
        self.assertIs(result[0]["entity"], input_entity)
        session.get_input_entity.assert_called_once()
        client.get_dialogs.assert_not_awaited()

    def test_restriction_like_errors_require_safety_stop(self):
        SessionRevokedError = type(
            "SessionRevokedError",
            (Exception,),
            {},
        )
        self.assertTrue(
            collector.telegram_error_requires_safety_stop(
                SessionRevokedError("revoked")
            )
        )
        self.assertTrue(
            collector.telegram_error_requires_safety_stop(
                RuntimeError("PEER_FLOOD")
            )
        )
        self.assertFalse(
            collector.telegram_error_requires_safety_stop(
                OSError("temporary network failure")
            )
        )

    def test_established_install_does_not_auto_reauthorize_missing_session(self):
        original_cred = collector.CRED_FILE
        original_session = collector.SESSION_FILE
        try:
            collector.CRED_FILE = self.directory / "credentials.bin"
            collector.CRED_FILE.write_bytes(b"existing")
            collector.SESSION_FILE = str(
                self.directory / "telegram_session"
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "telegram_session.session отсутствует",
            ):
                asyncio.run(
                    collector.ensure_telegram_client(
                        None,
                        {
                            "api_id": 1,
                            "api_hash": "a" * 32,
                            "phone": "+10000000000",
                        },
                        self.settings,
                    )
                )
        finally:
            collector.CRED_FILE = original_cred
            collector.SESSION_FILE = original_session

    def test_established_install_does_not_auto_reauthorize_unauthorized_session(self):
        original_cred = collector.CRED_FILE
        original_session = collector.SESSION_FILE
        try:
            collector.CRED_FILE = self.directory / "credentials.bin"
            collector.CRED_FILE.write_bytes(b"existing")
            collector.SESSION_FILE = str(
                self.directory / "telegram_session"
            )
            Path(collector.SESSION_FILE + ".session").write_bytes(
                b"existing-session"
            )

            candidate = SimpleNamespace(
                connect=AsyncMock(),
                is_user_authorized=AsyncMock(return_value=False),
                disconnect=AsyncMock(),
                start=AsyncMock(),
            )

            with patch.object(
                collector,
                "TelegramClient",
                return_value=candidate,
            ) as mocked_client:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "больше не авторизована",
                ):
                    asyncio.run(
                        collector.ensure_telegram_client(
                            None,
                            {
                                "api_id": 1,
                                "api_hash": "a" * 32,
                                "phone": "+10000000000",
                            },
                            self.settings,
                        )
                    )

            candidate.start.assert_not_awaited()
            self.assertGreaterEqual(candidate.disconnect.await_count, 1)
            self.assertEqual(
                mocked_client.call_args.kwargs["flood_sleep_threshold"],
                0,
            )
        finally:
            collector.CRED_FILE = original_cred
            collector.SESSION_FILE = original_session

    def test_explicit_reauthorization_can_start_new_session(self):
        original_cred = collector.CRED_FILE
        original_session = collector.SESSION_FILE
        try:
            collector.CRED_FILE = self.directory / "credentials.bin"
            collector.CRED_FILE.write_bytes(b"existing")
            collector.SESSION_FILE = str(
                self.directory / "telegram_session"
            )

            candidate = SimpleNamespace(
                connect=AsyncMock(),
                is_user_authorized=AsyncMock(return_value=False),
                disconnect=AsyncMock(),
                start=AsyncMock(),
            )

            with patch.object(
                collector,
                "TelegramClient",
                return_value=candidate,
            ):
                with patch.object(collector, "save_credentials"):
                    result_client, _ = asyncio.run(
                        collector.ensure_telegram_client(
                            None,
                            {
                                "api_id": 1,
                                "api_hash": "a" * 32,
                                "phone": "+10000000000",
                            },
                            self.settings,
                            allow_reauthorization=True,
                        )
                    )

            self.assertIs(result_client, candidate)
            candidate.start.assert_awaited_once()
        finally:
            collector.CRED_FILE = original_cred
            collector.SESSION_FILE = original_session

    def test_recreate_session_requires_confirmation_and_preserves_user_data(self):
        original_cred = collector.CRED_FILE
        original_session = collector.SESSION_FILE
        try:
            collector.CRED_FILE = self.directory / "credentials.bin"
            collector.CRED_FILE.write_bytes(b"existing-credentials")
            collector.SESSION_FILE = str(
                self.directory / "telegram_session"
            )
            session_file = Path(collector.SESSION_FILE + ".session")
            journal_file = Path(collector.SESSION_FILE + ".session-journal")
            session_file.write_bytes(b"session")
            journal_file.write_bytes(b"journal")

            client = SimpleNamespace(disconnect=AsyncMock())
            next_client = object()
            creds = {
                "api_id": 1,
                "api_hash": "a" * 32,
                "phone": "+10000000000",
            }

            with patch("builtins.input", return_value="1"):
                with patch.object(
                    collector,
                    "ensure_telegram_client",
                    AsyncMock(return_value=(next_client, creds)),
                ) as mocked_ensure:
                    result_client, result_creds = asyncio.run(
                        collector.recreate_telegram_session(
                            client,
                            creds,
                            self.settings,
                        )
                    )

            self.assertIs(result_client, next_client)
            self.assertEqual(result_creds, creds)
            self.assertFalse(session_file.exists())
            self.assertFalse(journal_file.exists())
            self.assertEqual(
                collector.CRED_FILE.read_bytes(),
                b"existing-credentials",
            )
            client.disconnect.assert_awaited_once()
            self.assertTrue(
                mocked_ensure.await_args.kwargs["allow_reauthorization"]
            )
        finally:
            collector.CRED_FILE = original_cred
            collector.SESSION_FILE = original_session

    def test_existing_bad_credentials_are_not_deleted_or_reprompted(self):
        original_cred = collector.CRED_FILE
        try:
            collector.CRED_FILE = self.directory / "credentials.bin"
            collector.CRED_FILE.write_bytes(b"existing-protected-data")

            with patch.object(
                collector,
                "dpapi_decrypt",
                side_effect=ValueError("cannot decrypt"),
            ):
                with patch("builtins.input") as mocked_input:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Автоматическая замена",
                    ):
                        collector.load_or_create_credentials()

            mocked_input.assert_not_called()
            self.assertTrue(collector.CRED_FILE.exists())
            self.assertEqual(
                collector.CRED_FILE.read_bytes(),
                b"existing-protected-data",
            )
        finally:
            collector.CRED_FILE = original_cred

    def test_ml_semantic_processing_is_disabled(self):
        self.assertFalse(
            collector.DEFAULT_SETTINGS["semantic_enabled"]
        )
        hits, meta = collector.semantic_candidates(
            self.connection,
            [],
            "проверка",
            self.settings,
        )
        self.assertEqual(hits, [])
        self.assertEqual(
            meta["reason"],
            "disabled_for_telegram_content_compliance",
        )

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
        duplicate = kept[0]["duplicates"][0]
        self.assertEqual(duplicate["message_id"], 2)
        self.assertEqual(duplicate["inherits_from_message_key"], "1:1")
        self.assertIn("text", duplicate["inherited_fields"])
        self.assertNotIn("text", duplicate)

    def test_exact_duplicate_with_different_change_status_is_still_collapsed(self):
        text = "Одинаковая публикация с достаточно длинным текстом для проверки статуса."
        base = {
            "channel_id": 1,
            "message_id": 1,
            "text": text,
            "date_utc": collector.iso_utc(self.now),
            "change_status": "existing",
        }
        other = dict(
            base,
            channel_id=2,
            message_id=2,
            change_status="new_since_previous_digest",
        )
        kept, exact, near = collector.collapse_duplicates(
            [base, other], self.settings
        )
        self.assertEqual((len(kept), exact, near), (1, 1, 0))
        self.assertEqual(
            kept[0]["duplicates"][0]["change_status"],
            "new_since_previous_digest",
        )

    def test_similar_but_distinct_text_is_preserved(self):
        kept, exact, near = self.collapse(
            "В городе открыли новую школу на улице Садовой.",
            "В городе открыли новую больницу на улице Садовой.",
        )
        self.assertEqual((len(kept), exact, near), (2, 0, 0))

    def test_new_name_is_not_removed_as_near_duplicate(self):
        prefix = "Подробное сообщение о состоявшейся встрече и принятом решении. " * 6
        kept, exact, near = self.collapse(
            prefix + "Комментарий дал Иван Петров.",
            prefix + "Комментарий дал Иван Сидоров.",
        )
        self.assertEqual((len(kept), exact, near), (2, 0, 0))

    def test_multilingual_text_survives_collection_sqlite_search_and_ai_export(self):
        samples = {
            1: "Україна посилює енергосистему перед зимою",
            2: "White House announced a new ceasefire framework",
        }
        for message_id, text in samples.items():
            msg = SimpleNamespace(
                id=message_id,
                message=text,
                date=self.now,
                edit_date=None,
                media=None,
                reactions=None,
                replies=None,
                fwd_from=None,
                grouped_id=None,
                reply_to_msg_id=None,
                post_author=None,
                views=None,
                forwards=None,
                buttons=None,
            )
            prepared = collector.make_message(
                self.channel,
                msg,
                self.settings,
            )
            self.assertEqual(prepared["text"], text)
            collector.upsert_message(self.connection, prepared)

        self.connection.commit()

        english_results = self.search("ceasefire")["direct_results"]
        self.assertEqual(
            [item["message_id"] for item in english_results],
            [2],
        )

        exported = collector.load_messages_for_export(
            self.connection,
            [self.channel],
            24,
            None,
            self.settings,
        )
        exported_by_id = {
            int(item["message_id"]): item["text"]
            for item in exported
        }
        self.assertEqual(exported_by_id, samples)

        json_text = __import__("json").dumps(
            [
                collector.prepare_message_for_ai(item)
                for item in exported
            ],
            ensure_ascii=False,
        )
        self.assertIn(samples[1], json_text)
        self.assertIn(samples[2], json_text)

    def test_eu_alias_is_searchable(self):
        self.add_message(1, "ЄС согласовал новое решение")
        self.assertEqual(len(self.search("ЕС")["direct_results"]), 1)

    def test_mobilization_does_not_match_mobile(self):
        self.add_message(1, "Объявлена мобилизация")
        self.add_message(2, "Новый мобильный телефон")
        results = self.search("мобилизация")["direct_results"]
        self.assertEqual([item["message_id"] for item in results], [1])

    def test_nova_poshta_aliases_keep_singular_brand_forms_without_plural_noise(self):
        positives = {
            1: "Новая почта открыла новое отделение",
            2: "В Новой почте сообщили о новом графике",
            3: "Решение Новой почты вступило в силу",
            4: "Новую почту временно закрыли",
            5: "Нова пошта відкрила нове відділення",
            6: "У Нової пошти змінився графік",
            7: "У Новій пошті повідомили про зміни",
            8: "Нову пошту відкрили у громаді",
            9: "Новою поштою відправили посилку",
            10: "Nova Poshta announced a new service",
        }
        negatives = {
            11: "У районі з'явилися нові пошти для різних сервісів",
            12: "З'явилися новые почты для тестовых аккаунтов",
            13: "Серед нових сервісів окремо згадали обмін файлами, а повідомлення надсилали електронною поштою",
        }
        for message_id, text in {**positives, **negatives}.items():
            self.add_message(message_id, text)

        results = self.search("Новая почта")["direct_results"]
        found_ids = {item["message_id"] for item in results}

        self.assertEqual(found_ids, set(positives))
        adjective_variants = collector.term_variants("новая", self.settings)
        for plural in ("новые", "новых", "нові", "нових"):
            self.assertNotIn(plural, adjective_variants)
        for singular in ("новая", "новой", "новую", "нова", "нової", "новій", "нову", "новою", "nova"):
            self.assertIn(singular, adjective_variants)

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

    def test_topic_search_builds_related_groups_without_fake_change_status(self):
        shared_url = "https://example.test/story/42"
        text = "Проверочная тема с общим первичным источником"

        self.add_message(1, text)
        self.connection.execute(
            """
            UPDATE messages
            SET canonical_urls_json = ?, origin_key = ?
            WHERE channel_id = 1 AND message_id = 1
            """,
            (
                __import__("json").dumps([shared_url]),
                "url:" + shared_url,
            ),
        )
        self.connection.execute(
            "INSERT INTO channels(channel_id, name, username) VALUES(2, 'Другой канал', 'other')"
        )
        self.connection.execute(
            """
            INSERT INTO messages(
                channel_id, message_id, text, date_utc, channel_name, username,
                canonical_urls_json, origin_key
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                2,
                text,
                collector.iso_utc(self.now - timedelta(minutes=1)),
                "Другой канал",
                "other",
                __import__("json").dumps([shared_url]),
                "url:" + shared_url,
            ),
        )
        self.connection.commit()

        result = collector.search_database(
            self.connection,
            "проверочная тема",
            1,
            self.settings,
            [1, 2],
        )
        self.assertEqual(len(result["direct_results"]), 2)
        self.assertEqual(len(result["related_message_groups"]), 1)
        for message in result["direct_results"]:
            self.assertNotIn("change_status", message)
            self.assertIn("previous_versions", message)

    def test_change_summary_counts_unavailable_separately(self):
        summary = collector.calculate_change_summary([
            {"change_status": "unavailable_since_previous_digest"}
        ])
        self.assertEqual(summary["unavailable_since_previous_digest"], 1)
        self.assertEqual(summary["existing"], 0)

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

    def test_public_forward_source_gets_direct_post_url(self):
        info = collector.enrich_forward_info_links({
            "chat_username": "source_channel",
            "channel_post": 321,
        })
        self.assertEqual(info["channel_url"], "https://t.me/source_channel")
        self.assertEqual(
            info["telegram_url"],
            "https://t.me/source_channel/321",
        )

    def test_derived_forward_links_do_not_create_false_content_edit(self):
        base = {
            "text": "Одинаковый текст",
            "media": {},
            "album_id": None,
            "reply_to_message_id": None,
            "post_author": None,
            "canonical_urls": [],
            "forwarded_from": {
                "chat_username": "source_channel",
                "channel_post": 321,
            },
        }
        enriched = dict(
            base,
            forwarded_from=collector.enrich_forward_info_links(
                base["forwarded_from"]
            ),
        )
        self.assertEqual(
            collector.semantic_content_hash(base),
            collector.semantic_content_hash(enriched),
        )

    def test_private_forward_source_does_not_invent_url(self):
        info = collector.enrich_forward_info_links({
            "from_id": {"type": "channel", "id": 123},
            "channel_post": 321,
        })
        self.assertNotIn("channel_url", info)
        self.assertNotIn("telegram_url", info)

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

    def test_bare_homepage_does_not_create_false_related_group(self):
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "canonical_urls": ["https://example.test"],
                "origin_key": "url:https://example.test",
            },
            {
                "channel_id": 2,
                "channel": "Канал B",
                "message_id": 20,
                "canonical_urls": ["https://example.test"],
                "origin_key": "url:https://example.test",
            },
        ]
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_max_profile_url_does_not_create_false_related_group(self):
        profile_url = "https://max.ru/SolovievLive"
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "canonical_urls": [profile_url],
            },
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 20,
                "canonical_urls": [profile_url],
            },
        ]
        self.assertIsNone(collector.make_origin_key(None, [profile_url]))
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_other_max_profile_url_does_not_create_false_related_group(self):
        profile_url = "https://max.ru/belarusian_silovik"
        messages = [
            {
                "channel_id": 2,
                "channel": "Белорусский силовик",
                "message_id": 11,
                "canonical_urls": [profile_url],
            },
            {
                "channel_id": 2,
                "channel": "Белорусский силовик",
                "message_id": 21,
                "canonical_urls": [profile_url],
            },
        ]
        self.assertIsNone(collector.make_origin_key(None, [profile_url]))
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_legacy_max_profile_origin_key_does_not_create_related_group(self):
        legacy_origin = "url:https://max.ru/SolovievLive"
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "canonical_urls": [],
                "origin_key": legacy_origin,
            },
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 20,
                "canonical_urls": [],
                "origin_key": legacy_origin,
            },
        ]
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_telegram_channel_root_is_not_specific_source(self):
        channel_url = "https://t.me/channel"
        self.assertFalse(collector.is_specific_shared_source_url(channel_url))
        self.assertIsNone(collector.make_origin_key(None, [channel_url]))

    def test_telegram_post_remains_specific_source(self):
        post_url = "https://t.me/channel/123"
        self.assertTrue(collector.is_specific_shared_source_url(post_url))
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "canonical_urls": [post_url],
            },
            {
                "channel_id": 2,
                "channel": "Канал B",
                "message_id": 20,
                "canonical_urls": [post_url],
            },
        ]
        self.assertEqual(len(collector.build_related_groups(messages)), 1)

    def test_external_article_remains_specific_source(self):
        article_url = "https://example.com/news/some-article"
        self.assertTrue(collector.is_specific_shared_source_url(article_url))
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "canonical_urls": [article_url],
            },
            {
                "channel_id": 2,
                "channel": "Канал B",
                "message_id": 20,
                "canonical_urls": [article_url],
            },
        ]
        self.assertEqual(len(collector.build_related_groups(messages)), 1)

    def test_recurring_service_url_in_one_channel_does_not_create_related_group(self):
        service_url = (
            "https://249860.redirect.appmetrica.yandex.com/"
            "?appmetrica_tracking_id=245880726195607142&referrer=reattribution%3D1"
        )
        messages = [
            {
                "channel_id": 100,
                "channel": "Один канал",
                "message_id": index,
                "canonical_urls": [service_url],
                "origin_key": "url:" + service_url,
            }
            for index in range(1, 31)
        ]
        self.assertFalse(collector.is_specific_shared_source_url(service_url))
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_same_specific_url_repeated_inside_one_channel_is_not_a_group(self):
        article_url = "https://example.com/news/concrete-article-2026"
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "canonical_urls": [article_url],
            },
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 11,
                "canonical_urls": [article_url],
            },
        ]
        self.assertTrue(collector.is_specific_shared_source_url(article_url))
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_exact_forward_origin_still_groups(self):
        origin = "telegram_forward:2012559840:7831"
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "origin_key": origin,
                "canonical_urls": [],
            },
            {
                "channel_id": 2,
                "channel": "Канал B",
                "message_id": 20,
                "origin_key": origin,
                "canonical_urls": [],
            },
        ]
        groups = collector.build_related_groups(messages)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["message_refs"]), 2)

    def test_similar_text_without_specific_source_is_not_related_group(self):
        messages = [
            {
                "channel_id": 1,
                "channel": "Канал A",
                "message_id": 10,
                "text": "Очень похожая формулировка одного сообщения",
                "canonical_urls": [],
            },
            {
                "channel_id": 2,
                "channel": "Канал B",
                "message_id": 20,
                "text": "Очень похожая формулировка другого сообщения",
                "canonical_urls": [],
            },
        ]
        self.assertEqual(collector.build_related_groups(messages), [])

    def test_generic_profile_category_and_query_only_urls_are_not_specific(self):
        for url in (
            "https://example.com/profile/reporter",
            "https://example.com/category/world",
            "https://example.com?campaign=always-on",
            "https://example.com/subscribe/newsletter",
        ):
            self.assertFalse(
                collector.is_specific_shared_source_url(url),
                msg=url,
            )

    def test_max_post_with_material_identifier_remains_specific_source(self):
        post_url = "https://max.ru/channel_vmax/AZ9FDVpHASw"
        self.assertTrue(collector.is_specific_shared_source_url(post_url))

    def test_continuity_loader_uses_matching_prior_digest_only(self):
        collector.ensure_dirs()
        channels = [{"id": 1, "name": "Test", "username": "test"}]
        fingerprint = collector.selection_fingerprint(channels)
        reference = collector.utc_now()
        period_start = reference - timedelta(hours=24)

        prior = {
            "channel_id": 1,
            "channel": "Test",
            "username": "test",
            "message_id": 10,
            "date_utc": collector.iso_utc(period_start - timedelta(hours=6)),
            "text": "Предыстория нужного сюжета",
        }
        current_period = {
            "channel_id": 1,
            "channel": "Test",
            "username": "test",
            "message_id": 20,
            "date_utc": collector.iso_utc(period_start + timedelta(hours=1)),
            "text": "Сообщение уже текущего периода",
        }

        good_payload = {
            "meta": {
                "artifact_type": "telegram_news_digest",
                "created_utc": collector.iso_utc(reference - timedelta(hours=1)),
                "selection_fingerprint": fingerprint,
            },
            "news_messages": [prior, current_period],
        }
        wrong_payload = {
            "meta": {
                "artifact_type": "telegram_news_digest",
                "created_utc": collector.iso_utc(reference - timedelta(hours=2)),
                "selection_fingerprint": "wrong-fingerprint",
            },
            "news_messages": [{
                **prior,
                "message_id": 30,
                "text": "Чужой набор каналов",
            }],
        }

        collector.LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        collector.LATEST_FILE.write_text(
            __import__("json").dumps(good_payload),
            encoding="utf-8",
        )
        (collector.ARCHIVE_DIR / "ДАЙДЖЕСТ_wrong.json").write_text(
            __import__("json").dumps(wrong_payload),
            encoding="utf-8",
        )

        loaded = collector.load_recent_continuity_messages(
            channels,
            period_start,
            reference_utc=reference,
        )

        self.assertEqual([item["message_id"] for item in loaded], [10])

    def test_continuity_lexical_followup_uses_bounded_prior_context(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "username": "a",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "date_local": "2026-09-18T11:00:00+03:00",
            "text": (
                "Компания Альфастрой объявила переговоры о покупке "
                "Северного машиностроительного завода"
            ),
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "username": "a",
            "message_id": 11,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "date_local": "2026-09-19T11:00:00+03:00",
            "text": (
                "Альфастрой подписала соглашение о покупке "
                "Северного машиностроительного завода"
            ),
        }

        context = collector.build_continuity_context(
            [current],
            [prior],
        )

        self.assertEqual(context["messages_count"], 1)
        item = context["messages"][0]
        self.assertEqual(item["context_message"]["message_id"], 10)
        relation = item["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "candidate")
        self.assertIn("lexical_candidate", relation["match_reasons"])

    def test_continuity_does_not_link_unrelated_posts_by_one_generic_word(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": "Закрытие завода Восток завершилось эвакуацией оборудования",
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 11,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": "Компания Запад открыла новый завод по выпуску аккумуляторов",
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_specific_source_is_strong_even_with_different_text(self):
        source_url = "https://example.com/news/story-42"
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": "Первое сообщение о событии",
            "canonical_urls": [source_url],
            "origin_key": "url:" + source_url,
        }
        current = {
            "channel_id": 2,
            "channel": "Канал B",
            "message_id": 20,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": "Совершенно другая формулировка продолжения",
            "canonical_urls": [source_url],
            "origin_key": "url:" + source_url,
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 1)
        relation = context["messages"][0]["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "strong")
        self.assertTrue(
            {"same_specific_origin", "same_specific_url"}
            & set(relation["match_reasons"])
        )

    def test_continuity_profile_url_does_not_create_context(self):
        profile_url = "https://max.ru/SolovievLive"
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": "Сообщение об одном происшествии",
            "canonical_urls": [profile_url],
            "origin_key": "url:" + profile_url,
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 11,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": "Совершенно другая экономическая новость",
            "canonical_urls": [profile_url],
            "origin_key": "url:" + profile_url,
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_reply_stays_strong_without_lexical_similarity(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "username": "channel_a",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": "Короткая заметка о переговорах компании",
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "username": "channel_a",
            "message_id": 11,
            "reply_to_message_id": 10,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": "Итог опубликован",
        }

        context = collector.build_continuity_context([current], [prior])
        relation = context["messages"][0]["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "strong")
        self.assertIn(
            "reply_to_previous_message",
            relation["match_reasons"],
        )

    def test_continuity_same_channel_telegram_self_link_is_not_strong(self):
        shared_url = "https://t.me/ZE_kartel/13821"
        prior = {
            "channel_id": 1447182889,
            "channel": "Картель",
            "username": "ZE_kartel",
            "channel_url": "https://t.me/ZE_kartel",
            "message_id": 13840,
            "date_utc": "2026-09-20T08:00:00+00:00",
            "text": (
                "Американский пакет вооружений включает средства ПВО "
                "и поставки для комплексов Patriot"
            ),
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }
        current = {
            "channel_id": 1447182889,
            "channel": "Картель",
            "username": "ZE_kartel",
            "channel_url": "https://t.me/ZE_kartel",
            "message_id": 13849,
            "date_utc": "2026-09-21T08:00:00+00:00",
            "text": (
                "Российские разработчики ускорили развитие новых "
                "беспилотных аппаратов и систем наведения"
            ),
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_private_telegram_self_link_is_not_strong(self):
        shared_url = "https://t.me/c/1447182889/13821"
        prior = {
            "channel_id": -1001447182889,
            "channel": "Приватный канал",
            "message_id": 13840,
            "date_utc": "2026-09-20T08:00:00+00:00",
            "text": "Материал о поставках медицинского оборудования",
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }
        current = {
            "channel_id": -1001447182889,
            "channel": "Приватный канал",
            "message_id": 13849,
            "date_utc": "2026-09-21T08:00:00+00:00",
            "text": "Совершенно отдельный материал о космической миссии",
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_second_real_self_link_pattern_is_not_strong(self):
        shared_url = "https://t.me/MediaKiller2021/24689"
        prior = {
            "channel_id": 1559000001,
            "channel": "MediaKiller",
            "username": "MediaKiller2021",
            "channel_url": "https://t.me/MediaKiller2021",
            "message_id": 24700,
            "date_utc": "2026-09-20T08:00:00+00:00",
            "text": (
                "После удара по Днепру обсуждается возможное применение "
                "нового типа ракеты Дань-Т"
            ),
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }
        current = {
            "channel_id": 1559000001,
            "channel": "MediaKiller",
            "username": "MediaKiller2021",
            "channel_url": "https://t.me/MediaKiller2021",
            "message_id": 24720,
            "date_utc": "2026-09-21T08:00:00+00:00",
            "text": (
                "Украинский перехватчик испытывают против реактивных "
                "беспилотников на новом полигоне"
            ),
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_same_channel_max_self_link_is_not_strong(self):
        shared_url = "https://max.ru/channel_vmax/AZ9FDVpHASw"
        prior = {
            "channel_id": 1,
            "channel": "channel_vmax",
            "username": "channel_vmax",
            "channel_url": "https://t.me/channel_vmax",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": "Материал о ремонте промышленного предприятия",
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }
        current = {
            "channel_id": 1,
            "channel": "channel_vmax",
            "username": "channel_vmax",
            "channel_url": "https://t.me/channel_vmax",
            "message_id": 11,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": "Отдельная публикация о космическом телескопе",
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_self_link_can_support_but_not_upgrade_lexical_match(self):
        shared_url = "https://t.me/ZE_kartel/13821"
        prior = {
            "channel_id": 1447182889,
            "channel": "Картель",
            "username": "ZE_kartel",
            "channel_url": "https://t.me/ZE_kartel",
            "message_id": 13840,
            "date_utc": "2026-09-20T08:00:00+00:00",
            "text": (
                "Patriot перехватчики американский пакет поставки "
                "противовоздушной обороны"
            ),
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }
        current = {
            "channel_id": 1447182889,
            "channel": "Картель",
            "username": "ZE_kartel",
            "channel_url": "https://t.me/ZE_kartel",
            "message_id": 13849,
            "date_utc": "2026-09-21T08:00:00+00:00",
            "text": (
                "Американский пакет поставки Patriot включает новые "
                "перехватчики противовоздушной обороны"
            ),
            "canonical_urls": [shared_url],
            "origin_key": "url:" + shared_url,
        }

        context = collector.build_continuity_context([current], [prior])
        relation = context["messages"][0]["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "candidate")
        self.assertIn("lexical_candidate", relation["match_reasons"])
        self.assertIn("same_specific_url", relation["match_reasons"])

    def test_continuity_common_language_noise_does_not_create_lexical_link(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": (
                "После авиаудара часть объектов повреждена, всего около "
                "десяти зданий, более точные данные появятся позже"
            ),
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 11,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": (
                "Советские компьютеры просто занимали более крупную часть "
                "помещения, всего около нескольких шкафов"
            ),
        }

        context = collector.build_continuity_context([current], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_vinnytsia_incident_survives_cross_channel_threshold(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": (
                "В Виннице местные жители отбили мужчину, которого сотрудники "
                "ТЦК пытались силой увезти в микроавтобусе"
            ),
        }
        current = {
            "channel_id": 2,
            "channel": "Канал B",
            "message_id": 20,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": (
                "В Виннице местные жители вмешались, когда сотрудники "
                "пытались силой доставить мужчину к микроавтобусу"
            ),
        }

        context = collector.build_continuity_context([current], [prior])
        relation = context["messages"][0]["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "candidate")
        self.assertIn("lexical_candidate", relation["match_reasons"])

    def test_continuity_voting_day_to_poll_closure_survives(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": (
                "Начался последний день голосования на парламентских выборах, "
                "избирательные участки открылись утром"
            ),
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 11,
            "date_utc": "2026-09-19T18:00:00+00:00",
            "text": (
                "Завершился последний день голосования на парламентских "
                "выборах: избирательные участки закрылись"
            ),
        }

        context = collector.build_continuity_context([current], [prior])
        relation = context["messages"][0]["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "candidate")

    def test_continuity_pelican_type_correction_survives(self):
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": (
                "Первоначально Pelican FP-7 назвали реактивной ракетой, "
                "дальность системы оценивали в 350 километров"
            ),
        }
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 11,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": (
                "Позже тип Pelican FP-7 уточнили: это беспилотная система, "
                "заявленная дальность составляет 350 километров"
            ),
        }

        context = collector.build_continuity_context([current], [prior])
        relation = context["messages"][0]["related_current_message_refs"][0]
        self.assertEqual(relation["match_strength"], "candidate")

    def test_continuity_repeated_short_air_alert_template_is_not_story(self):
        prior = []
        for message_id in range(10, 20):
            prior.append({
                "channel_id": 1,
                "channel": "Харьков",
                "username": "kharkiv",
                "message_id": message_id,
                "date_utc": f"2026-09-18T{message_id:02d}:00:00+00:00",
                "text": (
                    "Харків повітряна тривога негайно пройдіть в укриття"
                ),
            })
        current = {
            "channel_id": 1,
            "channel": "Харьков",
            "username": "kharkiv",
            "message_id": 30,
            "date_utc": "2026-09-19T08:00:00+00:00",
            "text": "Харків повітряна тривога негайно пройдіть в укриття",
        }

        context = collector.build_continuity_context([current], prior)
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_event_anchor_keeps_school_investigation_but_rejects_ai_topic_only(self):
        prior = {
            "channel_id": 1,
            "channel": "СОЛОВЬЁВ",
            "message_id": 10,
            "date_utc": "2026-09-20T08:00:00+00:00",
            "text": (
                "Bloomberg: США разбомбили иранскую школу из-за ошибки искусственного интеллекта\n\n"
                "Расследование выявило устаревшие разведданные, спутниковые снимки "
                "и чрезмерную опору на систему Maven Smart System от Palantir."
            ),
        }
        same_story = {
            "channel_id": 2,
            "channel": "Милитарист",
            "message_id": 20,
            "date_utc": "2026-09-21T10:12:02+00:00",
            "text": (
                "На прошедшей неделе Пентагон почти завершил расследование причин "
                "удара по детской школе в иранском Минабе. Bloomberg сообщает, "
                "что военные слишком сильно полагались на Maven Smart System "
                "компании Palantir и устаревшие разведывательные данные."
            ),
        }
        data_center = {
            "channel_id": 3,
            "channel": "Милитарист",
            "message_id": 30,
            "date_utc": "2026-09-21T11:58:45+00:00",
            "text": (
                "Дата-центр в штате Нью-Джерси случайно вылил тонны дизельного "
                "топлива в охраняемую водно-болотную территорию, что подчеркивает "
                "экологические риски инфраструктуры искусственного интеллекта.\n\n"
                "Системы искусственного интеллекта требуют крупной инфраструктуры, "
                "данных, вычислений и энергетических ресурсов."
            ),
        }
        background = []
        for offset in range(10):
            background.append({
                "channel_id": 100 + offset,
                "channel": f"AI фон {offset}",
                "message_id": 1000 + offset,
                "date_utc": f"2026-09-21T0{offset % 9}:00:00+00:00",
                "text": (
                    "Инфраструктура искусственного интеллекта развивается "
                    f"в отдельном секторе uniqueanchor{offset}"
                ),
            })

        context = collector.build_continuity_context(
            [same_story, data_center, *background],
            [prior],
        )
        refs = {
            ref["message_ref"]["message_id"]: ref
            for item in context["messages"]
            for ref in item["related_current_message_refs"]
            if item["context_message"]["message_id"] == 10
        }

        self.assertIn(20, refs)
        self.assertEqual(refs[20]["match_strength"], "candidate")
        self.assertNotIn(30, refs)

    def test_continuity_event_anchor_separates_novovolynsk_from_inter_kyiv(self):
        prior = {
            "channel_id": 1,
            "channel": "XUA",
            "message_id": 10,
            "date_utc": "2026-09-20T11:57:07+00:00",
            "text": (
                "В Нововолынске очевидцы сняли момент, как мужчину силой "
                "помещают в микроавтобус\n\n"
                "Несколько человек в военной форме удерживают мужчину и "
                "заталкивают его в микроавтобус."
            ),
        }
        same_story = {
            "channel_id": 2,
            "channel": "Канал B",
            "message_id": 20,
            "date_utc": "2026-09-21T08:00:00+00:00",
            "text": (
                "В Нововолынске мужчина пытался избежать принудительной посадки "
                "в микроавтобус, очевидцы вмешались в конфликт\n\n"
                "На кадрах люди в военной форме удерживают мужчину."
            ),
        }
        inter_kyiv = {
            "channel_id": 3,
            "channel": "Сплетница",
            "message_id": 30,
            "date_utc": "2026-09-21T09:00:00+00:00",
            "text": (
                "Мобилизационного конфликта не избежал представитель СМИ.\n\n"
                "Сотрудника телеканала Интер задержали возле студии в Киеве. "
                "На видео люди в военной форме удерживают мужчину и заталкивают "
                "его в машину."
            ),
        }

        context = collector.build_continuity_context(
            [same_story, inter_kyiv],
            [prior],
        )
        refs = {
            ref["message_ref"]["message_id"]
            for item in context["messages"]
            for ref in item["related_current_message_refs"]
            if item["context_message"]["message_id"] == 10
        }
        self.assertIn(20, refs)
        self.assertNotIn(30, refs)

    def test_continuity_event_anchor_does_not_merge_recurring_daily_regional_briefings(self):
        prior = {
            "channel_id": 1395451700,
            "channel": "Харьков life | Харків",
            "username": "kharkivlife",
            "message_id": 10,
            "date_utc": "2026-09-20T05:45:35+00:00",
            "text": (
                "Харківщина: за добу постраждали 4 людини\n\n"
                "Ворожих ударів зазнали Харків та 10 населених пунктів області. "
                "Пошкоджені будинки, автомобілі, енергомережі та склад."
            ),
        }
        next_day = {
            "channel_id": 1395451700,
            "channel": "Харьков life | Харків",
            "username": "kharkivlife",
            "message_id": 11,
            "date_utc": "2026-09-21T06:00:53+00:00",
            "text": (
                "Харківщина: за добу постраждали 8 людей\n\n"
                "Ворожих ударів зазнали Харків та 18 населених пунктів області. "
                "Пошкоджені будинки, автомобілі, електромережі та залізнична інфраструктура."
            ),
        }

        context = collector.build_continuity_context([next_day], [prior])
        self.assertEqual(context["messages_count"], 0)

    def test_continuity_event_anchor_keeps_conversion_center_reprints_but_rejects_other_crypto_fraud(self):
        prior = {
            "channel_id": 1,
            "channel": "INSIDER",
            "message_id": 10,
            "date_utc": "2026-09-20T13:28:39+00:00",
            "text": (
                "Разоблачён конвертационный центр, через который ежемесячно "
                "проходило около 500 млн грн, сообщили Офис Генпрокурора и БЭБ.\n\n"
                "Общий объем финансовых операций составил 23,5 млрд грн. "
                "Средства выводились через криптовалюту, участникам сообщили о подозрении."
            ),
        }
        reprint = {
            "channel_id": 2,
            "channel": "Шептун",
            "message_id": 20,
            "date_utc": "2026-09-21T08:00:00+00:00",
            "text": (
                "Офис Генпрокурора и БЭБ разоблачили конвертационный центр, "
                "через который ежемесячно проходило около 500 млн грн.\n\n"
                "Объем финансовых операций достиг 23,5 млрд грн, средства "
                "выводились через криптовалюту."
            ),
        }
        czech_fraud = {
            "channel_id": 3,
            "channel": "Канал C",
            "message_id": 30,
            "date_utc": "2026-09-21T09:00:00+00:00",
            "text": (
                "В Чехии раскрыли отдельное криптовалютное мошенничество "
                "с поддельной инвестиционной платформой.\n\n"
                "Следствие проверяет финансовые операции на миллионы, движение "
                "средств через криптовалюту и готовит подозрения участникам схемы."
            ),
        }

        context = collector.build_continuity_context(
            [reprint, czech_fraud],
            [prior],
        )
        refs = {
            ref["message_ref"]["message_id"]
            for item in context["messages"]
            for ref in item["related_current_message_refs"]
            if item["context_message"]["message_id"] == 10
        }
        self.assertIn(20, refs)
        self.assertNotIn(30, refs)

    def test_continuity_event_anchor_rejects_real_topic_only_false_positive_classes(self):
        cases = [
            (
                "locomotives_vs_polish_sirens",
                (
                    "Российский удар повредил украинские локомотивы в "
                    "железнодорожном депо.\n\n"
                    "Удары по транспортной инфраструктуре у границы усилили "
                    "риски, власти обсуждают безопасность и защиту объектов."
                ),
                (
                    "Польша установит автоматические сирены на границе с Украиной.\n\n"
                    "После ударов по инфраструктуре у границы власти усиливают "
                    "безопасность и защиту транспортных объектов."
                ),
            ),
            (
                "railway_vs_russian_speakers",
                (
                    "Повреждены украинские локомотивы и железнодорожная техника.\n\n"
                    "Украина обсуждает транспорт, инфраструктуру, государственную "
                    "политику и последствия войны для граждан."
                ),
                (
                    "Политик выступил с заявлением о русскоязычных гражданах Украины.\n\n"
                    "Украина обсуждает государственную политику, последствия войны, "
                    "инфраструктуру и транспорт для граждан."
                ),
            ),
            (
                "patriot_vs_global_diesel_shortage",
                (
                    "Украине не хватает ракет-перехватчиков Patriot для систем ПВО.\n\n"
                    "Дефицит поставок, сокращение запасов в США и растущая потребность "
                    "подталкивают цены и осложняют мировой рынок."
                ),
                (
                    "Глобальный дефицит дизельного топлива сохранится до следующего года.\n\n"
                    "Дефицит поставок, сокращение запасов в США и растущая потребность "
                    "подталкивают цены и осложняют мировой рынок."
                ),
            ),
            (
                "pika_vs_daily_ai_digest",
                (
                    "Pika полностью перезапустила платформу для создания контента с ИИ.\n\n"
                    "Платформа объединяет модели, генерацию видео, изображения, "
                    "контент и инструменты для пользователей."
                ),
                (
                    "Главные новости индустрии искусственного интеллекта за сутки.\n\n"
                    "Новые платформы и модели улучшают генерацию видео, изображения, "
                    "контент и инструменты для пользователей."
                ),
            ),
        ]

        for index, (label, prior_text, current_text) in enumerate(cases, start=1):
            with self.subTest(label=label):
                prior = {
                    "channel_id": 100 + index,
                    "channel": "Контекст",
                    "message_id": 10,
                    "date_utc": "2026-09-20T08:00:00+00:00",
                    "text": prior_text,
                }
                current = {
                    "channel_id": 200 + index,
                    "channel": "Текущее",
                    "message_id": 20,
                    "date_utc": "2026-09-21T08:00:00+00:00",
                    "text": current_text,
                }
                context = collector.build_continuity_context([current], [prior])
                self.assertEqual(context["messages_count"], 0)

    def test_continuity_event_anchor_keeps_real_same_event_classes(self):
        cases = [
            (
                "rassvet_beskrestnov",
                "Бескрестнов сообщил, что система Рассвет перехватила реактивный беспилотник над регионом.",
                "Заявление Бескрестнова о системе Рассвет: перехват реактивного беспилотника подтвержден новыми данными.",
            ),
            (
                "same_tusk_poll",
                "Опрос IBRiS показал изменение рейтинга правительства Дональда Туска среди польских избирателей.",
                "Тот же опрос IBRiS фиксирует рейтинг правительства Дональда Туска и настроения польских избирателей.",
            ),
            (
                "same_ft_metallurgy_story",
                "Financial Times сообщает о кризисе металлургических предприятий Украины и сокращении производства стали.",
                "По данным Financial Times, металлургические предприятия Украины сокращают производство стали из-за кризиса отрасли.",
            ),
            (
                "election_closure_to_count",
                "Избирательные участки закрылись после парламентских выборов, начался подсчет голосов и публикация первых данных.",
                "После закрытия избирательных участков продолжается подсчет голосов парламентских выборов, появились предварительные данные.",
            ),
            (
                "election_count_to_preliminary_results",
                "Продолжается подсчет голосов парламентских выборов, комиссии публикуют первые предварительные данные.",
                "Предварительные результаты парламентских выборов опубликованы после подсчета голосов большинством комиссий.",
            ),
            (
                "fuel_prices_kuyun",
                "Сергей Куюн заявил, что цены дизельного топлива на украинских АЗС продолжат расти из-за дефицита.",
                "Сергей Куюн сообщил новые данные: цены дизельного топлива на украинских АЗС выросли на фоне дефицита.",
            ),
            (
                "vivaldi_shandrigolovo",
                "Операция Вивальди в районе Шандриголово продолжается, подразделения сообщили о продвижении.",
                "Новые данные по операции Вивальди у Шандриголово: подразделения подтвердили дальнейшее продвижение.",
            ),
        ]

        for index, (label, prior_text, current_text) in enumerate(cases, start=1):
            with self.subTest(label=label):
                prior = {
                    "channel_id": 300 + index,
                    "channel": "Канал A",
                    "message_id": 10,
                    "date_utc": "2026-09-20T08:00:00+00:00",
                    "text": prior_text,
                }
                current = {
                    "channel_id": 400 + index,
                    "channel": "Канал B",
                    "message_id": 20,
                    "date_utc": "2026-09-21T08:00:00+00:00",
                    "text": current_text,
                }
                context = collector.build_continuity_context([current], [prior])
                self.assertEqual(
                    context["messages_count"],
                    1,
                    msg=label,
                )
                relation = context["messages"][0]["related_current_message_refs"][0]
                self.assertEqual(relation["match_strength"], "candidate", msg=label)
                self.assertIn("lexical_candidate", relation["match_reasons"], msg=label)

    def test_continuity_context_fanout_is_capped_and_strong_is_retained(self):
        groups = [
            ("alphaone", "betatwo", "gammathree"),
            ("deltaone", "epsilontwo", "zetathree"),
            ("etaalpha", "thetabeta", "iotagamma"),
            ("kappaone", "lambdatwo", "muthree"),
            ("nuone", "xitwo", "omicronthree"),
            ("pione", "rhotwo", "sigmathree"),
        ]
        prior = {
            "channel_id": 1,
            "channel": "Канал A",
            "username": "channel_a",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "text": " ".join(token for group in groups for token in group),
        }
        current = []
        for offset, group in enumerate(groups, start=1):
            current.append({
                "channel_id": 1,
                "channel": "Канал A",
                "username": "channel_a",
                "message_id": 10 + offset,
                "date_utc": f"2026-09-19T0{offset}:00:00+00:00",
                "text": " ".join(group),
            })
        current.append({
            "channel_id": 1,
            "channel": "Канал A",
            "username": "channel_a",
            "message_id": 99,
            "reply_to_message_id": 10,
            "date_utc": "2026-09-19T00:30:00+00:00",
            "text": "Отдельное прямое уточнение",
        })

        context = collector.build_continuity_context(current, [prior])
        links = context["messages"][0]["related_current_message_refs"]
        self.assertEqual(
            len(links),
            collector.CONTINUITY_RELATED_CURRENT_LIMIT,
        )
        self.assertEqual(links[0]["message_ref"]["message_id"], 99)
        self.assertEqual(links[0]["match_strength"], "strong")
        self.assertTrue(
            all(
                len(item["related_current_message_refs"])
                <= collector.CONTINUITY_RELATED_CURRENT_LIMIT
                for item in context["messages"]
            )
        )

    def test_continuity_context_is_bounded_per_current_message(self):
        source_url = "https://example.com/source/story-2026"
        current = {
            "channel_id": 1,
            "channel": "Канал A",
            "message_id": 50,
            "date_utc": "2026-09-20T08:00:00+00:00",
            "text": "Текущая публикация по общему первоисточнику",
            "canonical_urls": [source_url],
            "origin_key": "url:" + source_url,
        }
        prior = []
        for message_id, day in (
            (10, "17"),
            (11, "18"),
            (12, "19"),
        ):
            prior.append({
                "channel_id": 2,
                "channel": "Канал B",
                "message_id": message_id,
                "date_utc": f"2026-09-{day}T08:00:00+00:00",
                "text": f"Предыдущая публикация {message_id}",
                "canonical_urls": [source_url],
                "origin_key": "url:" + source_url,
            })

        context = collector.build_continuity_context(
            [current],
            prior,
            per_current=2,
        )

        self.assertEqual(context["messages_count"], 2)
        self.assertTrue(all(
            item["related_current_message_refs"][0]["match_strength"] == "strong"
            for item in context["messages"]
        ))

    def test_digest_exports_continuity_context_separately_from_current_period(self):
        channels = [{"id": 10, "name": "A", "username": "a"}]
        current = {
            "channel_id": 10,
            "channel": "A",
            "username": "a",
            "message_id": 20,
            "date_utc": collector.iso_utc(self.now),
            "date_local": collector.iso_local(self.now),
            "text": (
                "Альфастрой подписала соглашение о покупке "
                "Северного машиностроительного завода"
            ),
            "change_status": "new_since_previous_digest",
        }
        prior = {
            "channel_id": 10,
            "channel": "A",
            "username": "a",
            "message_id": 10,
            "date_utc": "2026-09-18T08:00:00+00:00",
            "date_local": "2026-09-18T11:00:00+03:00",
            "text": (
                "Альфастрой объявила переговоры о покупке "
                "Северного машиностроительного завода"
            ),
            "change_status": "existing",
            "related_group_id": "related_old",
        }
        sync_stats = {
            "new_messages_saved": 1,
            "content_changed_messages_refreshed": 0,
            "metrics_changed_messages_refreshed": 0,
            "migrated_messages": 0,
            "telegram_messages_scanned": 1,
            "failed_channels": 0,
            "successful_channels": 1,
            "channel_results": [],
            "history_completeness": {"complete": True},
            "self_diagnostics": {},
        }

        with patch.object(
            collector,
            "load_recent_continuity_messages",
            return_value=[prior],
        ):
            latest, _, _, _ = collector._v4_save_output(
                [current],
                [current],
                [],
                0,
                0,
                24,
                channels,
                sync_stats,
                "2026-09-18T12:00:00+00:00",
                "archive:test.json",
                [],
                self.settings,
            )

        payload = __import__("json").loads(
            latest.read_text(encoding="utf-8")
        )
        self.assertEqual(len(payload["news_messages"]), 1)
        self.assertEqual(payload["news_messages"][0]["message_id"], 20)
        self.assertEqual(
            payload["continuity_context"]["messages_count"],
            1,
        )
        context_message = (
            payload["continuity_context"]["messages"][0]["context_message"]
        )
        self.assertEqual(context_message["message_id"], 10)
        self.assertNotIn("change_status", context_message)
        self.assertNotIn("related_group_id", context_message)
        self.assertEqual(
            payload["meta"]["continuity_context_messages"],
            1,
        )
        self.assertEqual(
            payload["meta"]["news_messages_after_cleanup"],
            1,
        )

    def test_related_group_member_keeps_telegram_url(self):
        common_url = "https://example.test/news/source-2026"
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

    def test_prepare_message_for_ai_omits_only_redundant_raw_text(self):
        same = collector.prepare_message_for_ai({
            "channel_id": 1,
            "message_id": 1,
            "text": "Текст без удалённых строк",
            "raw_text": "Текст без удалённых строк",
            "raw_text_available": True,
        })
        self.assertNotIn("raw_text", same)
        self.assertNotIn("raw_text_available", same)

        compact_defaults = collector.prepare_message_for_ai({
            "channel_id": 1,
            "message_id": 10,
            "text": "Проверка пустых значений",
            "reactions": [],
            "external_urls": [],
            "canonical_urls": [],
            "duplicates": [],
            "media": {
                "type": None,
                "mime_type": None,
                "file_name": None,
                "identity": None,
            },
            "versions_count": 0,
            "versions_truncated": False,
            "previous_versions": [],
            "availability": "available",
            "availability_checked_utc": collector.iso_utc(self.now),
            "unavailable_since_utc": None,
            "in_selected_period": True,
        })
        for field in (
            "reactions",
            "external_urls",
            "canonical_urls",
            "duplicates",
            "media",
            "versions_count",
            "versions_truncated",
            "previous_versions",
            "availability",
            "availability_checked_utc",
            "unavailable_since_utc",
            "in_selected_period",
        ):
            self.assertNotIn(field, compact_defaults)

        different = collector.prepare_message_for_ai({
            "channel_id": 1,
            "message_id": 2,
            "text": "Содержательная часть",
            "raw_text": "Содержательная часть\nПодписаться на канал",
            "raw_text_available": True,
        })
        self.assertEqual(
            different["raw_text"],
            "Содержательная часть\nПодписаться на канал",
        )

    def test_revision_snapshot_captures_source_changes_without_db_noise(self):
        original = collector.make_message(
            self.channel,
            SimpleNamespace(
                id=777,
                message="Стабильный текст публикации для проверки истории редакций",
                date=self.now,
            ),
            self.settings,
        )
        original["external_urls"] = ["https://example.test/story/old"]
        original["canonical_urls"] = ["https://example.test/story/old"]
        original["origin_key"] = "url:https://example.test/story/old"
        collector.upsert_message(self.connection, original)

        updated = dict(original)
        updated["external_urls"] = ["https://example.test/story/new"]
        updated["canonical_urls"] = ["https://example.test/story/new"]
        updated["origin_key"] = "url:https://example.test/story/new"
        collector.upsert_message(self.connection, updated)
        self.connection.commit()

        row = self.connection.execute(
            "SELECT * FROM messages WHERE channel_id = 1 AND message_id = 777"
        ).fetchone()
        message = collector.db_row_to_message(row, None)
        collector.attach_versions(self.connection, [message], 3)

        self.assertEqual(message["versions_count"], 1)
        previous = message["previous_versions"][0]
        self.assertEqual(
            previous["canonical_urls"],
            ["https://example.test/story/old"],
        )
        self.assertNotIn("content_hash", previous)
        self.assertNotIn("metrics_hash", previous)
        self.assertNotIn("reactions_json", previous)

    def test_outside_period_edit_is_marked_as_change_context(self):
        baseline = collector.iso_utc(self.now - timedelta(hours=1))
        old_date = collector.iso_utc(self.now - timedelta(days=3))
        current_date = collector.iso_utc(self.now - timedelta(minutes=10))
        changed = collector.iso_utc(self.now - timedelta(minutes=5))
        first_seen = collector.iso_utc(self.now - timedelta(days=3))

        self.connection.execute(
            """
            INSERT INTO messages(
                channel_id, message_id, channel_name, username, date_utc,
                text, first_seen_utc, content_changed_utc
            ) VALUES(1, 801, 'Test', 'test', ?, 'Старая публикация', ?, ?)
            """,
            (old_date, first_seen, changed),
        )
        self.connection.execute(
            """
            INSERT INTO messages(
                channel_id, message_id, channel_name, username, date_utc,
                text, first_seen_utc
            ) VALUES(1, 802, 'Test', 'test', ?, 'Текущая публикация', ?)
            """,
            (current_date, current_date),
        )
        self.connection.commit()

        messages = collector.load_messages_for_export(
            self.connection,
            [self.channel],
            24,
            baseline,
            self.settings,
        )
        by_id = {message["message_id"]: message for message in messages}
        self.assertFalse(by_id[801]["in_selected_period"])
        self.assertEqual(
            by_id[801]["change_status"],
            "edited_since_previous_digest",
        )
        self.assertTrue(by_id[802]["in_selected_period"])

    def test_digest_export_keeps_outside_period_changes_out_of_main_arrays(self):
        old_change = {
            "channel_id": 1,
            "message_id": 901,
            "message_key": "1:901",
            "channel": "Test",
            "username": "test",
            "date_utc": collector.iso_utc(self.now - timedelta(days=3)),
            "date_local": collector.iso_local(self.now - timedelta(days=3)),
            "text": "Старая публикация получила существенную правку",
            "raw_text": "Старая публикация получила существенную правку",
            "raw_text_available": True,
            "in_selected_period": False,
            "change_status": "edited_since_previous_digest",
        }
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
        latest, _, _, _ = collector._v4_save_output(
            [old_change],
            [],
            [],
            0,
            0,
            24,
            [self.channel],
            sync_stats,
            collector.iso_utc(self.now - timedelta(hours=1)),
            "news.db:runs",
            [],
            self.settings,
        )
        payload = __import__("json").loads(
            latest.read_text(encoding="utf-8")
        )
        self.assertEqual(payload["news_messages"], [])
        changes = payload["changes_since_previous_digest"]
        self.assertEqual(changes["outside_period_change_count"], 1)
        self.assertEqual(
            changes["outside_period_changes"][0]["message_key"],
            "1:901",
        )

    def test_digest_request_uses_telegram_urls_for_sources(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("SOURCE_URL", request)
        self.assertIn("telegram_url", request)
        self.assertIn("channel_url", request)
        self.assertIn("Markdown-ссылкой", request)
        self.assertIn("копируй ДОСЛОВНО", request)
        self.assertIn("URL не придумывай", request)
        self.assertIn("Google или redirect-ссылкой", request)
        self.assertIn("не «исправляй» по памяти", request)
        self.assertIn("исходную literal URL", request)
        self.assertIn("Каждый самостоятельный фактический сюжет завершай строкой источника", request)
        self.assertIn("для составного — 2–3 ключевых", request)

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
        self.assertIn("не используй общие знания модели", request)
        self.assertIn("не добавляй существенные факты, географию", request)
        self.assertIn("участников, мотивы, последствия или причинность", request)
        self.assertIn("если данных для вывода недостаточно", request)
        self.assertIn("перепечатки одного исходного сообщения не считай независимыми подтверждениями", request)
        self.assertIn("количество публикаций само по себе не делает сюжет важнее", request)
        for confidence in (
            "подтверждено",
            "вероятно",
            "пока не подтверждено",
            "спорно",
            "мнение/оценка",
        ):
            self.assertIn(confidence, request)
        self.assertIn("similar_message_refs", request)
        self.assertIn("это не доказательство одного события", request)

    def test_digest_request_handles_mixed_languages_without_separate_buckets(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("на языке запроса пользователя", request)
        self.assertIn("если язык запроса не указан или неясен, пиши по-русски", request)
        self.assertIn("Иноязычные публикации переводи по смыслу", request)
        self.assertIn("сохраняя имена, числа, цитируемые факты", request)
        self.assertIn("Не разделяй источники по языку", request)
        self.assertIn(
            "русско-, украино- и англоязычные сообщения одного события объединяй в один сюжет",
            request,
        )

    def test_digest_request_keeps_adaptive_compact_structure(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("«Главное за период»", request)
        self.assertIn("не ставь перед ним второй абзац с тем же резюме", request)
        self.assertIn("Однотипные оперативные предупреждения одного сюжета объединяй", request)
        self.assertIn("changes_since_previous_digest.comparison_available=true", request)
        self.assertIn("глубину каждого сюжета определяй количеством реально новой информации", request)
        self.assertIn("не ограничивай этим число сюжетов", request)
        self.assertIn("собери в «Коротко» вместо того, чтобы опустить их", request)
        self.assertNotIn("при среднем объёме", request.lower())
        self.assertNotIn("при большом", request.lower())

    def test_digest_request_is_completeness_first(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("Сначала учти весь набор текущих сообщений", request)
        self.assertIn("Не заканчивай дайджест после нескольких самых заметных историй", request)
        self.assertIn("Каждый самостоятельный содержательно значимый сюжет", request)
        self.assertIn("полнота важной повестки важнее искусственной краткости", request)
        self.assertIn("Одиночное важное сообщение нельзя терять", request)

    def test_related_groups_are_only_a_hint_in_prompt(self):
        request = collector.DIGEST_REQUEST
        self.assertIn(
            "related_message_groups — только подсказка о возможном общем источнике",
            request,
        )
        self.assertIn("не приказ объединять сообщения в одно событие", request)

    def test_digest_comparison_never_replaces_full_period(self):
        request = collector.DIGEST_REQUEST
        self.assertIn("Основной дайджест всегда строй по всему содержательному материалу", request)
        self.assertIn("changes_since_previous_digest — только дополнительный слой сравнения", request)
        self.assertIn("не задаёт временные границы основного дайджеста", request)
        self.assertIn("не является фильтром отбора", request)
        self.assertIn("не исключай из основного дайджеста", request)
        self.assertIn("outside_period_changes", request)
        self.assertIn("не расширяй ими основной временной интервал", request)
        self.assertIn("continuity_context", request)
        self.assertIn("lexical_candidate не доказательство", request)
        self.assertIn("context_message не выдавай за текущую новость", request)

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

    def test_source_rules_use_clickable_literal_urls(self):
        rules = collector.SOURCE_RULES
        self.assertIn("Каждый самостоятельный фактический сюжет", rules)
        self.assertIn("источники должны покрывать существенные утверждения", rules)
        self.assertIn("иначе раздели сюжет", rules)
        self.assertIn("В «Коротко» ставь источник после каждого события", rules)
        self.assertIn("«Главное за период» может не дублировать ссылки", rules)
        self.assertIn("используй ровно эту строку", rules)
        self.assertIn("кликабельной Markdown-ссылкой", rules)
        self.assertIn("**Источник:** [Канал](https://t.me/...)", rules)
        self.assertIn("дословно совпадать с исходным", rules)
        self.assertIn("Google, search или redirect-ссылкой", rules)
        self.assertIn("utm_source", rules)
        self.assertIn("не сокращай", rules)
        self.assertIn("не нормализуй", rules)
        self.assertIn("не меняй query", rules)
        self.assertIn("не «исправляй» по памяти", rules)
        self.assertIn("исходную literal URL как цель Markdown-ссылки", rules)
        self.assertIn("для составного — 2–3 ключевых", rules)

    def test_editorial_rules_prioritize_late_updates_preserve_certainty_and_avoid_topic_merge(self):
        rules = collector.EDITORIAL_PRINCIPLES
        self.assertIn("самое позднее состояние по времени", rules)
        self.assertIn("позднее уточнение имеет приоритет", rules)
        self.assertIn(
            "Первоначально сообщалось…, позже выяснилось…",
            rules,
        )
        for marker in (
            "опровергает",
            "блокирует",
            "уточняет",
            "отменяет",
            "меняет статус",
        ):
            self.assertIn(marker, rules)
        self.assertIn("Не повышай уверенность относительно источника", rules)
        self.assertIn("«возможно»", rules)
        self.assertIn("«по данным источника»", rules)
        self.assertIn("«предположительно»", rules)
        self.assertIn(
            "общая тема, страна или организация сами по себе не означают одно событие",
            rules,
        )


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
        self.assertEqual(collector.EXPORT_SCHEMA_VERSION, 8)
        self.assertEqual(collector.DIGEST_PROFILE_VERSION, "8.8")
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"schema_version": EXPORT_SCHEMA_VERSION', source)
        self.assertIn('"digest_profile_version": DIGEST_PROFILE_VERSION', source)


    def _candidate_message(
        self,
        channel_id,
        message_id,
        minute,
        text,
        *,
        channel=None,
        username=None,
        **extra,
    ):
        username = username or f"channel_{channel_id}"
        result = {
            "channel_id": channel_id,
            "message_id": message_id,
            "message_key": f"{channel_id}:{message_id}",
            "date_local": f"2026-09-22T10:{minute:02d}:00+03:00",
            "channel": channel or f"Channel {channel_id}",
            "username": username,
            "telegram_url": f"https://t.me/{username}/{message_id}",
            "text": text,
        }
        result.update(extra)
        return result

    def test_event_candidate_singleton_is_never_lost(self):
        message = self._candidate_message(
            1, 1, 10, "Unique cobalt observatory report."
        )
        layer = collector.build_event_candidates([message])
        self.assertEqual(layer["coverage"]["input_current_messages"], 1)
        self.assertEqual(layer["coverage"]["event_candidates"], 1)
        self.assertEqual(layer["coverage"]["singleton_candidates"], 1)
        self.assertEqual(
            layer["candidates"][0]["member_refs"][0]["message_key"],
            "1:1",
        )
        self.assertEqual(layer["candidates"][0]["relation"], "singleton")

    def test_event_candidate_assigns_every_current_message_exactly_once(self):
        messages = [
            self._candidate_message(1, 1, 10, "Amber archive notice alpha."),
            self._candidate_message(2, 2, 20, "Cobalt registry notice beta."),
            self._candidate_message(3, 3, 30, "Indigo terminal notice gamma."),
        ]
        layer = collector.build_event_candidates(messages)
        refs = [
            ref["message_key"]
            for candidate in layer["candidates"]
            for ref in candidate["member_refs"]
        ]
        self.assertEqual(sorted(refs), ["1:1", "2:2", "3:3"])
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(layer["coverage"]["unassigned_messages"], 0)
        self.assertEqual(layer["coverage"]["duplicate_assignments"], 0)

    def test_event_candidate_common_forward_is_strong_group(self):
        messages = [
            self._candidate_message(
                1, 1, 10, "First wording from a forwarded bulletin.",
                origin_key="telegram_forward:777:42",
            ),
            self._candidate_message(
                2, 2, 20, "Second wording from the same forwarded bulletin.",
                origin_key="telegram_forward:777:42",
            ),
        ]
        layer = collector.build_event_candidates(messages)
        self.assertEqual(len(layer["candidates"]), 1)
        self.assertEqual(layer["candidates"][0]["relation"], "strong_source")
        self.assertIn(
            "telegram_forward_origin",
            layer["candidates"][0]["relation_reasons"],
        )

    def test_event_candidate_exact_article_source_is_strong_across_channels(self):
        article = "https://example.com/news/material-4242"
        messages = [
            self._candidate_message(
                1,
                1,
                10,
                "One channel references a concrete external material.",
                canonical_urls=[article],
            ),
            self._candidate_message(
                2,
                2,
                20,
                "Another channel references the same concrete material.",
                canonical_urls=[article],
            ),
        ]
        layer = collector.build_event_candidates(messages)
        self.assertEqual(len(layer["candidates"]), 1)
        self.assertEqual(layer["candidates"][0]["relation"], "strong_source")
        self.assertIn(
            "same_specific_external_source",
            layer["candidates"][0]["relation_reasons"],
        )

    def test_event_candidate_reply_different_counterparty_stays_separate(self):
        scheduled = self._candidate_message(
            1,
            1,
            10,
            "Президент Арлен встретится с Бореком завтра.",
        )
        held_with_other = self._candidate_message(
            1,
            2,
            20,
            "Президент Арлен встретился с Корвином сегодня.",
            reply_to_message_id=1,
        )

        layer = collector.build_event_candidates(
            [scheduled, held_with_other]
        )

        self.assertEqual(len(layer["candidates"]), 2)
        self.assertEqual(layer["coverage"]["input_current_messages"], 2)
        self.assertEqual(layer["coverage"]["unassigned_messages"], 0)
        self.assertEqual(layer["coverage"]["duplicate_assignments"], 0)
        assigned = {
            ref["message_key"]
            for candidate in layer["candidates"]
            for ref in candidate["member_refs"]
        }
        self.assertEqual(assigned, {"1:1", "1:2"})

    def test_event_candidate_reply_same_counterparty_remains_strong(self):
        scheduled = self._candidate_message(
            1,
            1,
            10,
            "Президент Арлен встретится с Бореком завтра.",
        )
        held_update = self._candidate_message(
            1,
            2,
            20,
            "Президент Арлен встретился с Бореком и обсудил торговлю.",
            reply_to_message_id=1,
        )

        layer = collector.build_event_candidates([scheduled, held_update])

        self.assertEqual(len(layer["candidates"]), 1)
        self.assertEqual(
            layer["candidates"][0]["relation"],
            "strong_source",
        )
        self.assertIn(
            "reply_to_current_message",
            layer["candidates"][0]["relation_reasons"],
        )

    def test_event_candidate_ordinary_reply_remains_strong(self):
        notice = self._candidate_message(
            1,
            1,
            10,
            "Порт закрыт из-за шторма.",
        )
        update = self._candidate_message(
            1,
            2,
            20,
            "Уточнение: движение судов возобновится вечером.",
            reply_to_message_id=1,
        )

        layer = collector.build_event_candidates([notice, update])

        self.assertEqual(len(layer["candidates"]), 1)
        self.assertIn(
            "reply_to_current_message",
            layer["candidates"][0]["relation_reasons"],
        )

    def test_event_candidate_counterparty_veto_does_not_weaken_other_strong_relations(self):
        left_text = "Президент Арлен встретится с Бореком завтра."
        right_text = "Президент Арлен встретился с Корвином сегодня."
        article = "https://example.com/news/material-counterparty"

        cases = [
            (
                "forward",
                self._candidate_message(
                    1,
                    1,
                    10,
                    left_text,
                    origin_key="telegram_forward:777:42",
                ),
                self._candidate_message(
                    2,
                    2,
                    20,
                    right_text,
                    origin_key="telegram_forward:777:42",
                ),
                "telegram_forward_origin",
            ),
            (
                "external_source",
                self._candidate_message(
                    1,
                    3,
                    10,
                    left_text,
                    canonical_urls=[article],
                ),
                self._candidate_message(
                    2,
                    4,
                    20,
                    right_text,
                    canonical_urls=[article],
                ),
                "same_specific_external_source",
            ),
            (
                "related_group",
                self._candidate_message(
                    1,
                    5,
                    10,
                    left_text,
                    related_group_id="related_test",
                ),
                self._candidate_message(
                    2,
                    6,
                    20,
                    right_text,
                    related_group_id="related_test",
                ),
                "related_message_group",
            ),
        ]

        for label, first, second, reason in cases:
            with self.subTest(label=label):
                layer = collector.build_event_candidates([first, second])
                self.assertEqual(len(layer["candidates"]), 1)
                self.assertIn(
                    reason,
                    layer["candidates"][0]["relation_reasons"],
                )

    def test_event_candidate_lexical_different_explicit_meetings_stay_separate(self):
        first = self._candidate_message(
            1,
            1,
            20,
            "Встреча Арлена с Бореком возможна, заявил Марко Рубио.",
        )
        second = self._candidate_message(
            1,
            2,
            10,
            "Встреча Кадена с Дорианом ожидается, заявил Марко Рубио.",
        )

        layer = collector.build_event_candidates([first, second])

        self.assertEqual(len(layer["candidates"]), 2)
        self.assertEqual(layer["coverage"]["unassigned_messages"], 0)
        self.assertEqual(layer["coverage"]["duplicate_assignments"], 0)

    def test_event_candidate_same_meeting_pair_still_lexically_merges(self):
        first = self._candidate_message(
            1,
            1,
            20,
            "Встреча Арлена с Бореком состоится завтра в столице.",
        )
        second = self._candidate_message(
            1,
            2,
            10,
            "Встреча Арлена с Бореком запланирована на завтра в столице.",
        )

        layer = collector.build_event_candidates([first, second])

        self.assertEqual(len(layer["candidates"]), 1)
        self.assertEqual(
            layer["candidates"][0]["relation"],
            "lexical_candidate",
        )

    def test_event_candidate_publisher_credit_is_not_same_channel_event_anchor(self):
        first = self._candidate_message(
            1,
            1,
            20,
            "Orion council approved harbor budget, сообщает Frank Media "
            "со ссылкой на источник.",
        )
        second = self._candidate_message(
            1,
            2,
            10,
            "Lumen holding appointed finance director, сообщает Frank Media "
            "со ссылкой на источник.",
        )

        layer = collector.build_event_candidates([first, second])

        self.assertEqual(len(layer["candidates"]), 2)

    def test_event_candidate_publisher_credit_cannot_attach_unrelated_cross_channel_story(self):
        budget_new = self._candidate_message(
            1,
            1,
            30,
            "Budget talks face political collapse in Lumen — Financial Times\n\n"
            "The possibility of agreement is significantly restricted, "
            "the report writes.",
        )
        budget_old = self._candidate_message(
            2,
            2,
            20,
            "Budget talks face political collapse in Lumen after regional vote.\n\n"
            "The possibility of agreement is significantly restricted.",
        )
        unrelated = self._candidate_message(
            3,
            3,
            10,
            "Diesel export restriction remains unlikely in Orion — Financial Times\n\n"
            "The possibility of action is significantly restricted, "
            "the report writes.",
        )

        layer = collector.build_event_candidates(
            [budget_new, budget_old, unrelated]
        )
        memberships = {
            frozenset(
                ref["message_key"]
                for ref in candidate["member_refs"]
            )
            for candidate in layer["candidates"]
        }

        self.assertEqual(
            memberships,
            {
                frozenset({"1:1", "2:2"}),
                frozenset({"3:3"}),
            },
        )

    def test_event_candidate_near_duplicates_keep_one_full_evidence_and_supporting_ref(self):
        older = self._candidate_message(
            1, 1, 10, "Nearly identical bulletin text retained once."
        )
        newer = self._candidate_message(
            2,
            2,
            20,
            "Nearly identical bulletin text retained once!",
            similar_message_refs=["1:1"],
        )
        layer = collector.build_event_candidates([older, newer])
        candidate = layer["candidates"][0]
        self.assertEqual(candidate["messages_count"], 2)
        self.assertEqual(len(candidate["evidence_messages"]), 1)
        self.assertEqual(len(candidate["supporting_refs"]), 1)
        self.assertEqual(
            candidate["evidence_messages"][0]["message_key"],
            "2:2",
        )
        self.assertEqual(
            candidate["supporting_refs"][0]["message_key"],
            "1:1",
        )
        self.assertEqual(
            candidate["supporting_refs"][0]["retained_as"],
            "2:2",
        )
        self.assertEqual(
            layer["coverage"]["near_duplicate_supporting_refs"],
            1,
        )

    def test_event_candidate_lexical_anchor_can_join_obvious_episode_continuation(self):
        messages = [
            self._candidate_message(
                1,
                1,
                20,
                "Quasar reactor chamber pressure collapse inspection continues.",
            ),
            self._candidate_message(
                1,
                2,
                10,
                "Quasar reactor chamber pressure collapse inspection started.",
            ),
        ]
        layer = collector.build_event_candidates(messages)
        self.assertEqual(len(layer["candidates"]), 1)
        self.assertEqual(
            layer["candidates"][0]["relation"],
            "lexical_candidate",
        )

    def test_event_candidate_broad_common_topic_does_not_merge(self):
        messages = [
            self._candidate_message(
                1,
                1,
                20,
                "Meridian corporation opened robotics laboratory in Tallinn.",
            ),
            self._candidate_message(
                1,
                2,
                10,
                "Meridian corporation announced dividend policy for shareholders.",
            ),
        ]
        layer = collector.build_event_candidates(messages)
        self.assertEqual(len(layer["candidates"]), 2)

    def test_event_candidate_lexical_chain_does_not_transitively_bridge(self):
        messages = [
            self._candidate_message(
                1,
                1,
                30,
                "Amber falcon harbor closure inspection continues.",
            ),
            self._candidate_message(
                1,
                2,
                20,
                "Amber falcon harbor closure inspection cobalt runway reopening.",
            ),
            self._candidate_message(
                1,
                3,
                10,
                "Cobalt runway reopening weather review continues.",
            ),
        ]
        layer = collector.build_event_candidates(messages)
        sizes = sorted(
            candidate["messages_count"]
            for candidate in layer["candidates"]
        )
        self.assertEqual(sizes, [1, 2])
        first_candidate_keys = {
            ref["message_key"]
            for ref in layer["candidates"][0]["member_refs"]
        }
        self.assertEqual(first_candidate_keys, {"1:1", "1:2"})
        self.assertNotIn("1:3", first_candidate_keys)

    def test_event_candidate_two_events_of_same_organization_do_not_merge(self):
        messages = [
            self._candidate_message(
                1,
                1,
                20,
                "Atlas foundation opened coastal archive in Lisbon.",
            ),
            self._candidate_message(
                1,
                2,
                10,
                "Atlas foundation appointed finance director in Warsaw.",
            ),
        ]
        layer = collector.build_event_candidates(messages)
        self.assertEqual(len(layer["candidates"]), 2)

    def test_event_candidate_ru_en_without_strong_relation_do_not_force_merge(self):
        messages = [
            self._candidate_message(
                1,
                1,
                20,
                "Станция сообщила о завершении ремонта северного терминала.",
            ),
            self._candidate_message(
                2,
                2,
                10,
                "The station announced a new research grant for marine biology.",
            ),
        ]
        layer = collector.build_event_candidates(messages)
        self.assertEqual(len(layer["candidates"]), 2)

    def test_event_candidate_literal_source_urls_survive_candidate_export(self):
        first = self._candidate_message(
            1,
            1,
            10,
            "Literal source alpha remains.",
        )
        second = self._candidate_message(
            2,
            2,
            20,
            "Literal source beta remains.",
            similar_message_refs=["1:1"],
        )
        second["telegram_url"] = "https://t.me/channel_2/2?x=KeepCase"
        payload = {
            "meta": {},
            "news_messages": [first, second],
            "operational_messages": [],
            "continuity_context": {"messages": []},
            "changes_since_previous_digest": {"outside_period_changes": []},
        }
        rendered = collector.render_ai_friendly_markdown(payload)
        self.assertIn("https://t.me/channel_1/1", rendered)
        self.assertIn("https://t.me/channel_2/2?x=KeepCase", rendered)
        self.assertIn("SUPPORTING_REFS:", rendered)

    def test_event_candidate_order_is_newest_activity_first(self):
        older = self._candidate_message(
            1, 1, 10, "Older independent candidate."
        )
        newer = self._candidate_message(
            2, 2, 30, "Newer independent candidate."
        )
        layer = collector.build_event_candidates([older, newer])
        self.assertEqual(
            layer["candidates"][0]["member_refs"][0]["message_key"],
            "2:2",
        )
        self.assertEqual(
            layer["candidates"][1]["member_refs"][0]["message_key"],
            "1:1",
        )

    def test_candidate_renderer_does_not_mutate_canonical_payload(self):
        payload = {
            "meta": {"digest_profile_version": "8.2"},
            "news_messages": [
                self._candidate_message(
                    1, 1, 10, "Immutable canonical payload message."
                )
            ],
            "operational_messages": [],
            "continuity_context": {"messages": []},
            "changes_since_previous_digest": {"outside_period_changes": []},
        }
        before = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        collector.render_ai_friendly_markdown(payload)
        after = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertEqual(after, before)

    def test_candidate_renderer_uses_current_profile_for_saved_canonical(self):
        payload = {
            "meta": {
                "digest_profile_version": "8.2",
                "recommended_digest_request": "old saved request",
            },
            "news_messages": [
                self._candidate_message(
                    1, 1, 10, "Saved canonical can be re-rendered."
                )
            ],
            "operational_messages": [],
            "continuity_context": {"messages": []},
            "changes_since_previous_digest": {"outside_period_changes": []},
        }
        rendered = collector.render_ai_friendly_markdown(payload)
        self.assertIn("DIGEST_PROFILE: 8.8", rendered)
        self.assertIn(collector.CANDIDATE_GUIDANCE, rendered)
        self.assertNotIn("old saved request", rendered)

    def test_ai_markdown_is_newest_first_and_omits_textless_media_only(self):
        payload = {
            "meta": {
                "digest_profile_version": collector.DIGEST_PROFILE_VERSION,
                "recommended_digest_request": collector.DIGEST_REQUEST,
            },
            "news_messages": [
                {
                    "channel_id": 1,
                    "message_id": 10,
                    "message_key": "1:10",
                    "date_local": "2026-09-22T10:00:00+03:00",
                    "channel": "Русский канал",
                    "username": "ru_channel",
                    "telegram_url": "https://t.me/ru_channel/10?x=LiteralCase",
                    "text": "Русский текст без сокращения.",
                },
                {
                    "channel_id": 2,
                    "message_id": 20,
                    "message_key": "2:20",
                    "date_local": "2026-09-22T12:00:00+03:00",
                    "channel": "Український канал",
                    "username": "ua_channel",
                    "telegram_url": "https://t.me/ua_channel/20",
                    "text": "Змістовний підпис до відео.",
                    "media": {"type": "video"},
                },
                {
                    "channel_id": 3,
                    "message_id": 30,
                    "message_key": "3:30",
                    "date_local": "2026-09-22T11:00:00+03:00",
                    "channel": "Media only",
                    "username": "media_only",
                    "telegram_url": "https://t.me/media_only/30",
                    "text": "[Медиа без подписи: photo]",
                    "media": {"type": "photo"},
                },
            ],
            "operational_messages": [],
            "continuity_context": {"messages": []},
            "changes_since_previous_digest": {"outside_period_changes": []},
        }

        rendered = collector.render_ai_friendly_markdown(payload)

        self.assertIn("INPUT_CURRENT_MESSAGES: 2", rendered)
        self.assertIn("EVENT_CANDIDATES: 2", rendered)
        self.assertIn("UNASSIGNED_MESSAGES: 0", rendered)
        self.assertIn("DUPLICATE_ASSIGNMENTS: 0", rendered)
        self.assertIn("OMITTED_MEDIA_ONLY: 1", rendered)
        self.assertIn(
            "LOCAL_INTERVAL: 2026-09-22T10:00:00+03:00 — "
            "2026-09-22T12:00:00+03:00",
            rendered,
        )
        self.assertEqual(rendered.count("[MESSAGE 1:10]"), 1)
        self.assertEqual(rendered.count("[MESSAGE 2:20]"), 1)
        self.assertNotIn("[MESSAGE 3:30]", rendered)
        self.assertNotIn("[Медиа без подписи: photo]", rendered)
        self.assertIn("Русский текст без сокращения.", rendered)
        self.assertIn("Змістовний підпис до відео.", rendered)
        self.assertIn("MEDIA: video", rendered)
        self.assertIn("DATE_LOCAL: 2026-09-22T10:00:00+03:00", rendered)
        self.assertIn("DATE_LOCAL: 2026-09-22T12:00:00+03:00", rendered)
        self.assertLess(
            rendered.index("[MESSAGE 2:20]"),
            rendered.index("[MESSAGE 1:10]"),
        )
        self.assertIn(
            "SOURCE_URL: https://t.me/ru_channel/10?x=LiteralCase",
            rendered,
        )
        self.assertNotIn("google.com", rendered.lower())


    def test_save_output_keeps_canonical_media_only_but_ai_markdown_omits_it(self):
        messages = [
            {
                "channel_id": 1,
                "message_id": 101,
                "message_key": "1:101",
                "channel": "Test",
                "username": "test",
                "date_utc": collector.iso_utc(self.now - timedelta(minutes=3)),
                "date_local": collector.iso_local(self.now - timedelta(minutes=3)),
                "telegram_url": "https://t.me/test/101",
                "text": "Первое содержательное сообщение.",
                "in_selected_period": True,
                "change_status": "first_digest",
            },
            {
                "channel_id": 1,
                "message_id": 102,
                "message_key": "1:102",
                "channel": "Test",
                "username": "test",
                "date_utc": collector.iso_utc(self.now - timedelta(minutes=2)),
                "date_local": collector.iso_local(self.now - timedelta(minutes=2)),
                "telegram_url": "https://t.me/test/102",
                "text": "Содержательная подпись к видео.",
                "media": {"type": "video"},
                "in_selected_period": True,
                "change_status": "first_digest",
            },
            {
                "channel_id": 1,
                "message_id": 103,
                "message_key": "1:103",
                "channel": "Test",
                "username": "test",
                "date_utc": collector.iso_utc(self.now - timedelta(minutes=1)),
                "date_local": collector.iso_local(self.now - timedelta(minutes=1)),
                "telegram_url": "https://t.me/test/103",
                "text": "[Медиа без подписи: photo]",
                "media": {"type": "photo"},
                "in_selected_period": True,
                "change_status": "first_digest",
            },
        ]
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

        latest, _, _, _ = collector._v4_save_output(
            messages,
            messages,
            [],
            0,
            0,
            24,
            [self.channel],
            sync_stats,
            None,
            None,
            [],
            self.settings,
        )

        canonical = json.loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(len(canonical["news_messages"]), 3)
        self.assertEqual(
            [item["message_key"] for item in canonical["news_messages"]],
            ["1:101", "1:102", "1:103"],
        )
        self.assertEqual(
            canonical["news_messages"][2]["text"],
            "[Медиа без подписи: photo]",
        )
        self.assertTrue(collector.AI_LATEST_FILE.exists())
        ai_text = collector.AI_LATEST_FILE.read_text(encoding="utf-8")
        self.assertIn("[MESSAGE 1:101]", ai_text)
        self.assertIn("[MESSAGE 1:102]", ai_text)
        self.assertNotIn("[MESSAGE 1:103]", ai_text)
        self.assertIn("Первое содержательное сообщение.", ai_text)
        self.assertIn("Содержательная подпись к видео.", ai_text)
        self.assertNotIn("[Медиа без подписи: photo]", ai_text)


    def test_ai_export_filters_recurring_service_urls_but_keeps_article_urls_literal(self):
        message = {
            "channel_id": 1,
            "message_id": 1,
            "message_key": "1:1",
            "channel": "Test",
            "date_local": "2026-09-22T10:00:00+03:00",
            "telegram_url": "https://t.me/test/1",
            "text": "Текст",
            "external_urls": [
                "https://249860.redirect.appmetrica.yandex.com/?appmetrica_tracking_id=123",
                "https://example.com/news/concrete-article-2026?utm_source=telegram",
            ],
        }
        urls = collector._ai_material_external_urls(message)
        self.assertEqual(
            urls,
            ["https://example.com/news/concrete-article-2026?utm_source=telegram"],
        )

    def test_export_contract_keeps_user_controlled_ai_handoff(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"recommended_digest_request"', source)
        self.assertNotIn('"recommended_ai_request"', source)
        self.assertIn('"content_use_notice"', source)
        self.assertIn('"usage_hint"', source)
        self.assertIn("например ChatGPT", source)
        self.assertIn("не отправляет", source)
        self.assertTrue(
            hasattr(collector, "build_search_digest_instruction")
        )
        self.assertFalse(
            hasattr(collector, "build_search_ai_instruction")
        )
        self.assertNotIn("ФАЙЛ ДЛЯ ИИ-АССИСТЕНТА", source)

    def test_readme_documents_local_only_export_and_policy_review(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "Программа сама не отправляет содержимое Telegram во внешние AI/ML-сервисы",
            readme,
        )
        self.assertIn(
            "Смысловой ML/embedding-поиск по Telegram-контенту",
            readme,
        )
        self.assertIn(
            "Sponsored Messages",
            readme,
        )
        self.assertIn(
            "Unofficial TelegramNewsAI",
            readme,
        )
        self.assertIn(
            "полный JSON + плоский Markdown для ИИ → выбранный пользователем ИИ → дайджест событий",
            readme,
        )
        self.assertIn(
            "T` — явно пересоздать Telegram-сессию",
            readme,
        )
        self.assertNotIn("--setup-semantic", readme)

    def test_readme_and_testing_plan_use_50_source_product_limit(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        testing = (
            ROOT / "docs" / "TELEGRAM_API_TESTING.md"
        ).read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("максимум **50 выбранных", readme)
        self.assertIn("максимум 50", testing)
        self.assertNotIn("около 60 каналов", testing)
        self.assertNotIn("около 75 каналов", testing)
        self.assertNotIn("около 100 каналов", testing)
        self.assertNotIn("свыше 100 каналов", readme)
        self.assertNotIn("свыше 100 каналов", security)

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

    def test_installer_checks_path_python_before_recursive_fallback(self):
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        fast = installer.index("# Fast path:")
        fallback = installer.index("# Fallback only")
        self.assertLess(fast, fallback)
        self.assertIn("return $resolved", installer[fast:fallback])

    def test_user_facing_branding_is_unofficial(self):
        self.assertEqual(
            collector.APP_DISPLAY_NAME,
            "Unofficial TelegramNewsAI",
        )
        launcher = (
            ROOT / "launcher" / "Telegram_Digest.cs"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'AssemblyTitle("Unofficial TelegramNewsAI")',
            launcher,
        )
        build_launcher = (
            ROOT / "launcher" / "build_launcher.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("/win32icon:", build_launcher)
        self.assertNotIn("assets\\icon.png", build_launcher)
        self.assertTrue((ROOT / "assets" / "icon.svg").exists())
        self.assertTrue((ROOT / "launcher" / "build_icon.ps1").exists())

    def test_custom_icon_builder_outputs_multisize_ico_contract(self):
        builder = (
            ROOT / "launcher" / "build_icon.ps1"
        ).read_text(encoding="utf-8")
        for size in ("16", "24", "32", "48", "64", "128", "256"):
            self.assertIn(size, builder)
        self.assertIn("System.Drawing", builder)
        self.assertIn("BinaryWriter", builder)

    def test_readme_displays_project_icon(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('src="assets/icon.svg"', readme)
        self.assertIn('width="220"', readme)

    def test_readme_stable_status_matches_release_contract(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(collector.APP_VERSION, "5.5.0 Stable")
        self.assertIn(
            "Текущая версия — 5.5.0 Stable",
            readme,
        )
        self.assertIn(
            "первый принятый Stable для личного сценария TelegramNewsAI",
            readme,
        )
        self.assertIn("schema 8", readme)
        self.assertIn("digest profile 8.8", readme)
        self.assertIn("Telethon остаётся production-транспортом", readme)
        self.assertIn(
            "точные критерии и лимиты не раскрываются",
            readme,
        )
        self.assertNotIn("личное использование и стабильность", readme)
        self.assertNotIn("Если потеря доступа к конкретному аккаунту", readme)

    def test_release_builder_uses_exact_maintenance_distribution(self):
        builder = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8-sig")
        manifest = __import__("json").loads(
            (ROOT / "release_manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("ConvertFrom-Json", builder)
        self.assertIn("release_manifest.json", builder)
        for required in ("LICENSE", "SECURITY.md", "Telegram_Digest.exe.sha256", "UPDATE.bat", "update.ps1"):
            self.assertIn(required, manifest["ProgramFiles"])
        self.assertIn("Unofficial-TelegramNewsAI-$version-$channel-Windows", builder)
        self.assertIn("launcher\\build_launcher.ps1", builder)
        self.assertNotIn(".gitignore", manifest["ProgramFiles"])

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
        request = collector.build_search_digest_instruction(
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

    def test_topic_search_json_exports_digest_profile_version(self):
        self.add_message(1, "Нова пошта відкрила нове відділення")
        result = self.search("Новая почта")
        collector.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        collector.SEARCH_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        latest, _, payload = collector._v4_save_search_output(
            self.connection,
            "Новая почта",
            1,
            self.settings,
            {"quick_check_ok": True, "quick_check_result": "ok"},
            channels=[self.channel],
            history_status={"complete": True},
            search_result=result,
        )
        written = __import__("json").loads(latest.read_text(encoding="utf-8"))

        self.assertEqual(
            payload["meta"]["digest_profile_version"],
            collector.DIGEST_PROFILE_VERSION,
        )
        self.assertEqual(
            written["meta"]["digest_profile_version"],
            collector.DIGEST_PROFILE_VERSION,
        )
        self.assertEqual(
            written["meta"]["schema_version"],
            collector.EXPORT_SCHEMA_VERSION,
        )

    def test_editorial_requests_do_not_depend_on_chatgpt_ui(self):
        requests = collector.DIGEST_REQUEST + collector.build_search_digest_instruction(
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
        self.assertEqual(payload["meta"]["schema_version"], 8)
        self.assertEqual(
            payload["meta"]["digest_profile_version"],
            collector.DIGEST_PROFILE_VERSION,
        )
        self.assertIn("raw_text_representation", payload["meta"])
        self.assertIn("recommended_digest_request", payload["meta"])
        self.assertNotIn("recommended_ai_request", payload["meta"])
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
