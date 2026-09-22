# -*- coding: utf-8 -*-
"""Unofficial TelegramNewsAI — локальный сбор, история и поиск Telegram-публикаций.
Дайджест загружает новые сообщения и недостающую историю выбранного периода.
Поиск предлагает обновление; локальный режим показывает актуальность базы.
Полные тексты сохраняются без квот на каналы и без ограничения выдачи.
"""

import asyncio
import copy
import html
import sys
import math
import unicodedata
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from collections import Counter
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from getpass import getpass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from telethon import TelegramClient
    from telethon.errors import ApiIdInvalidError, FloodWaitError
    from telethon.tl.types import Channel, PeerChannel
except ImportError:
    print("Telethon не установлен.")
    print("Выполните: python -m pip install --upgrade telethon")
    input("Нажмите Enter для выхода...")
    raise SystemExit(1)

APP_VERSION = "5.5.0 Testing"
APP_DISPLAY_NAME = "Unofficial TelegramNewsAI"

# Версии экспортируемого JSON независимы от версии приложения.
# Схема 8 не повторяет одинаковые text/raw_text и отделяет изменения
# старых публикаций от основного временного окна дайджеста.
EXPORT_SCHEMA_VERSION = 8
DIGEST_PROFILE_VERSION = "8.7"
MAX_SELECTED_CHANNELS = 50

APP_DIR = Path(__file__).resolve().parent
CRED_FILE = APP_DIR / "credentials.bin"
SELECTION_FILE = APP_DIR / "selected_channels.json"
SESSION_FILE = str(APP_DIR / "telegram_session")
SETTINGS_FILE = APP_DIR / "settings_free.json"
DB_FILE = APP_DIR / "news.db"

OUTPUT_DIR = APP_DIR / "Дайджесты"
ARCHIVE_DIR = OUTPUT_DIR / "Архив"
RAW_DIR = ARCHIVE_DIR / "Сырые"
LOG_DIR = APP_DIR / "logs"

LATEST_FILE = OUTPUT_DIR / "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.json"
AI_LATEST_FILE = OUTPUT_DIR / "ДАЙДЖЕСТ_ДЛЯ_ИИ.md"
SEARCH_LATEST_FILE = OUTPUT_DIR / "ПОИСК_ПОСЛЕДНИЙ.json"
SEARCH_ARCHIVE_DIR = ARCHIVE_DIR / "Поиск"

# Короткая локальная предыстория для продолжающихся сюжетов.
# Используются только уже сохранённые сопоставимые дайджесты:
# никаких дополнительных Telegram-запросов и фоновой обработки.
CONTINUITY_LOOKBACK_DAYS = 3
CONTINUITY_MAX_MESSAGES = 40
CONTINUITY_PER_CURRENT_MESSAGE = 2
CONTINUITY_SOURCE_DIGESTS = 3
CONTINUITY_RELATED_CURRENT_LIMIT = 5
CONTINUITY_EVENT_ANCHOR_LEAD_TOKENS = 28
CONTINUITY_EVENT_ANCHOR_PAIR_WINDOW = 4


# ============================================================
# Windows DPAPI
# ============================================================

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(data: bytes):
    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf


def dpapi_encrypt(data: bytes) -> bytes:
    in_blob, _ = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "TelegramNewsAI",
        None, None, None, 0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_decrypt(data: bytes) -> bytes:
    in_blob, _ = _blob_from_bytes(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None, None, None, 0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ============================================================
# Settings / logging
# ============================================================

DEFAULT_SETTINGS = {
    "default_hours": 24,

    "remove_exact_duplicates": True,
    "remove_near_duplicates": True,
    "near_duplicate_threshold": 0.985,
    "near_duplicate_window_hours": 3,

    "separate_routine_alerts": True,
    "strip_promo_lines": True,
    "include_media_only_messages": True,

    "save_raw_backup": False,

    "refresh_recent_messages": 50,

    "channel_retry_attempts": 3,
    "retry_delay_seconds": 3,
    # В Testing-ветке любой FloodWait должен быть виден приложению:
    # Telethon ничего не пережидает и не повторяет скрыто от нашего кода.
    "max_flood_wait_seconds": 0,
    "telethon_flood_sleep_threshold_seconds": 0,
    "stop_on_any_flood_wait": True,

    # Консервативный последовательный профиль для поддерживаемого сценария
    # максимум из 50 выбранных источников. Это инженерный default проекта,
    # а не официальный safe limit Telegram.
    "history_request_wait_seconds": 0.5,
    "inter_channel_delay_seconds": 1.0,
    "flood_wait_safety_seconds": 5,

    "open_output_folder": True,

    "archive_retention_days": 7,
    "raw_retention_days": 1,
    "log_retention_days": 7,

    "database_retention_days": 90,
    "max_database_mb": 500,
    "database_target_mb_after_prune": 450,

    "sqlite_quick_check_interval_days": 7,
    "sqlite_optimize_on_close": False,

    "search_default_days": 30,
    "search_max_results": 1500,
    "search_related_context_limit": 0,
    "search_prefix_length": 5,
    "refresh_recent_hours": 2,
    "deletion_check_limit": 0,
    "open_html_preview": False,
    "revision_export_limit": 3,
    "search_freshness_minutes": 15,

    # Короткие многословные запросы не расширяются до
    # бессмысленного OR. Для 3+ слов разрешается только контролируемое
    # частичное совпадение с минимальным покрытием терминов.
    "search_partial_coverage": 0.67,
    "search_partial_candidate_limit": 10000,

    # ML/embedding-поиск по Telegram-контенту отключён.
    # Локальный поиск остаётся на SQLite FTS5/LIKE без внешних моделей.
    "semantic_enabled": False
}


def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    SEARCH_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_settings():
    ensure_dirs()

    data = {}
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass

    result = DEFAULT_SETTINGS.copy()
    result.update(data)

    # Однократно мигрируем только известные штатные Testing-профили.
    # Пользовательские pacing-значения не переписываем.
    legacy_bulk_profile = (
        result.get("history_request_wait_seconds") == 1.0
        and result.get("inter_channel_delay_seconds") == 0.75
        and result.get("bulk_channel_threshold") == 50
        and result.get("bulk_inter_channel_delay_seconds") == 1.5
    )
    legacy_standard_profile = (
        data.get("history_request_wait_seconds") == 0.5
        and data.get("inter_channel_delay_seconds") == 0.25
        and data.get("telethon_flood_sleep_threshold_seconds") == 0
        and data.get("max_flood_wait_seconds") == 0
        and data.get("stop_on_any_flood_wait") is True
        and data.get("refresh_recent_messages") == 50
        and data.get("refresh_recent_hours") in (2, 2.0)
    )
    if legacy_bulk_profile:
        result.update({
            "history_request_wait_seconds": 0.5,
            "inter_channel_delay_seconds": 1.0,
        })
    elif legacy_standard_profile:
        result["inter_channel_delay_seconds"] = 1.0
    result.pop("bulk_channel_threshold", None)
    result.pop("bulk_inter_channel_delay_seconds", None)

    # Эти ограничения не дают обычному поиску повторно сканировать Telegram
    # и загружать тяжёлую нейронную модель без явной необходимости.
    result.update({
        "search_related_context_limit": 0,
        "deletion_check_limit": 0,
        "open_html_preview": False,
        "refresh_recent_messages": max(0, min(100, int(result.get("refresh_recent_messages", 50)))),
        "refresh_recent_hours": max(0, float(result.get("refresh_recent_hours", 2))),
        # Защитные настройки нельзя случайно отключить пользовательским
        # settings_free.json: FloodWait остаётся видимым приложению, а
        # нагрузка после первого серверного ограничения не продолжается.
        "max_flood_wait_seconds": 0,
        "telethon_flood_sleep_threshold_seconds": 0,
        "stop_on_any_flood_wait": True,
        "history_request_wait_seconds": max(
            0.5,
            min(
                5.0,
                float(result.get("history_request_wait_seconds", 0.5)),
            ),
        ),
        "inter_channel_delay_seconds": max(
            0.0,
            min(
                10.0,
                float(result.get("inter_channel_delay_seconds", 1.0)),
            ),
        ),
        "flood_wait_safety_seconds": max(
            1,
            min(
                60,
                int(result.get("flood_wait_safety_seconds", 5)),
            ),
        ),
    })

    # Не позволяем старому settings_free.json снова включить ML-поиск.
    result["semantic_enabled"] = False
    for obsolete_semantic_key in (
        "semantic_model",
        "semantic_max_messages",
        "semantic_max_results",
        "semantic_min_score",
        "semantic_trigger_below",
    ):
        result.pop(obsolete_semantic_key, None)

    # Записываем новые параметры в существующий settings_free.json,
    # не требуя от пользователя заменять этот файл вручную.
    try:
        SETTINGS_FILE.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return result


def setup_logging():
    ensure_dirs()
    log_path = LOG_DIR / f"collector_{datetime.now().strftime('%Y-%m-%d')}.log"
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger("TelegramNewsAI")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    # Telethon пишет сетевые предупреждения через logging. Без своего
    # обработчика Python отправляет WARNING/ERROR прямо в консоль,
    # хотя библиотека часто сама восстанавливает соединение.
    telethon_logger = logging.getLogger("telethon")
    telethon_logger.setLevel(logging.WARNING)
    telethon_logger.propagate = False
    if not telethon_logger.handlers:
        telethon_handler = logging.FileHandler(log_path, encoding="utf-8")
        telethon_handler.setFormatter(formatter)
        telethon_logger.addHandler(telethon_handler)

    return logger


LOGGER = logging.getLogger("TelegramNewsAI")


def log_info(message):
    LOGGER.info(message)


def log_error(message):
    LOGGER.error(message)


def history_request_wait_seconds(settings):
    """Умеренная пауза между последовательными запросами истории."""
    try:
        value = float(
            (settings or DEFAULT_SETTINGS).get(
                "history_request_wait_seconds",
                0.5,
            )
        )
    except (TypeError, ValueError):
        value = 0.5
    return max(0.5, min(5.0, value))


def inter_channel_delay_seconds(settings, channel_count):
    """Пауза между каналами в поддерживаемом диапазоне до 50 источников."""
    source = settings or DEFAULT_SETTINGS
    try:
        normal = float(
            source.get(
                "inter_channel_delay_seconds",
                1.0,
            )
        )
    except (TypeError, ValueError):
        normal = 1.0

    # channel_count сохраняется в сигнатуре для совместимости и журналирования.
    # Отдельного скрытого профиля для 100+ каналов больше нет.
    _ = channel_count
    return max(0.0, min(10.0, normal))


def flood_wait_safety_seconds(settings):
    try:
        value = int(
            (settings or DEFAULT_SETTINGS).get(
                "flood_wait_safety_seconds",
                5,
            )
        )
    except (TypeError, ValueError):
        value = 5
    return max(1, min(60, value))


SECURITY_RPC_MARKERS = (
    "PEER_FLOOD",
    "AUTH_KEY",
    "AUTHKEY",
    "SESSION_REVOKED",
    "SESSIONREVOKED",
    "SESSION_EXPIRED",
    "SESSIONEXPIRED",
    "USER_DEACTIVATED",
    "USERDEACTIVATED",
    "PHONE_NUMBER_BANNED",
    "PHONENUMBERBANNED",
    "FROZEN_METHOD_INVALID",
    "FROZEN_PARTICIPANT_MISSING",
    "USER_RESTRICTED",
    "USERRESTRICTED",
    "UNAUTHORIZED",
)


def telegram_error_requires_safety_stop(error):
    """True для ограничений/авторизации, которые нельзя слепо ретраить."""
    combined = (
        f"{type(error).__name__} {error}"
    ).upper()

    if "FLOOD" in combined:
        return True

    return any(
        marker in combined
        for marker in SECURITY_RPC_MARKERS
    )


# ============================================================
# Time / JSON helpers
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_utc(dt):
    if not dt:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def iso_local(dt):
    if not dt:
        return None
    return dt.astimezone().isoformat(timespec="seconds")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def unique_keep_order(values):
    out = []
    seen = set()
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


BACK_COMMANDS = {
    "0",
    "назад",
    "back",
    "отмена",
    "cancel",
}


def is_back_command(value):
    """Единая команда возврата из меню и незавершённого ввода."""
    return str(value or "").strip().casefold() in BACK_COMMANDS


# ============================================================
# Credentials
# ============================================================

def prompt_telegram_code():
    return input("\nВведите код подтверждения из Telegram: ").strip()


def prompt_telegram_password():
    print("\nTelegram запросил пароль двухэтапной защиты.")
    print(
        "Ввод скрыт: символы на экране не отображаются — это нормально."
    )
    return getpass(
        "Введите или вставьте пароль и нажмите Enter: "
    )


def is_valid_api_hash(value):
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{32}",
            str(value or "").strip(),
        )
    )


def prompt_api_hash():
    print(
        "\nAPI Hash вводится скрыто: "
        "символы на экране не отображаются — это нормально."
    )
    print(
        "Если вставка в скрытое поле сработала неправильно, "
        "используйте Shift+Insert или правую кнопку мыши."
    )

    while True:
        api_hash = getpass(
            "Введите или вставьте API Hash и нажмите Enter: "
        ).strip()

        if is_valid_api_hash(api_hash):
            return api_hash

        print(
            f"API Hash введён некорректно: длина {len(api_hash)}, "
            "ожидается 32 символа 0-9/a-f."
        )

        if len(api_hash) <= 1:
            print(
                "Похоже, сочетание клавиш вставило управляющий символ "
                "вместо текста. Попробуйте Shift+Insert или правую кнопку мыши."
            )


def save_credentials(data):
    CRED_FILE.write_bytes(
        dpapi_encrypt(
            json.dumps(
                data,
                ensure_ascii=False,
            ).encode("utf-8")
        )
    )


def load_or_create_credentials():
    if CRED_FILE.exists():
        try:
            raw = dpapi_decrypt(CRED_FILE.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise RuntimeError(
                "Не удалось прочитать существующий credentials.bin. "
                "Автоматическая замена сохранённых Telegram API-данных "
                "отключена. Не удаляйте файл наугад: сначала проверьте "
                "резервную копию и текущую Telegram-сессию. "
                f"Техническая причина: {type(e).__name__}: {e}"
            ) from e

        if (
            isinstance(data, dict)
            and isinstance(data.get("api_id"), int)
            and data.get("api_id", 0) > 0
            and is_valid_api_hash(data.get("api_hash"))
            and str(data.get("phone") or "").strip()
        ):
            return data

        raise RuntimeError(
            "Существующий credentials.bin имеет неверный формат. "
            "Автоматическое удаление и повторный ввод отключены, чтобы "
            "не создавать случайную новую авторизацию. Сначала проверьте "
            "резервную копию и состояние Telegram-сессии."
        )

    print("\n=== Первичная настройка Telegram ===")
    print(
        "\nПрограмма подключается через неофициальный Telegram API-клиент. "
        "Telegram может ограничивать такую активность, а универсальные "
        "безопасные лимиты не публикует."
    )
    print(
        "Запросы выполняются последовательно; при FloodWait или сигнале "
        "ограничения текущий сетевой этап останавливается.\n"
    )

    while True:
        confirmation = input(
            "Enter — продолжить; 0 — выйти: "
        ).strip()
        if is_back_command(confirmation):
            raise SystemExit(0)
        if not confirmation:
            break
        print("Нажмите Enter для продолжения или 0 для выхода.")

    print("Для подключения нужны API ID и API Hash.")
    print(
        "Получить их можно: "
        "https://my.telegram.org → API development tools."
    )
    print(
        "API ID — число, API Hash — 32 символа из цифр и букв a-f.\n"
    )

    while True:
        api_id_text = input("Введите API ID: ").strip()
        if api_id_text.isdigit() and int(api_id_text) > 0:
            break
        print(
            "API ID должен состоять из цифр. Попробуйте ещё раз."
        )

    api_hash = prompt_api_hash()
    phone = input(
        "\nВведите номер Telegram в международном формате, "
        "например +380...: "
    ).strip()

    return {
        "api_id": int(api_id_text),
        "api_hash": api_hash,
        "phone": phone,
    }


# ============================================================
# Channel selection
# ============================================================

def channel_item(entity, name=None):
    username = getattr(entity, "username", None)
    title = (
        name
        or getattr(entity, "title", None)
        or username
        or str(getattr(entity, "id", ""))
    )
    return {
        "id": int(entity.id),
        "name": title,
        "username": username,
        "entity": entity,
    }


def load_selection():
    if not SELECTION_FILE.exists():
        return []

    try:
        data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_selection(items):
    SELECTION_FILE.write_text(
        json.dumps(
            [
                {
                    "id": int(x["id"]),
                    "name": x["name"],
                    "username": x.get("username"),
                }
                for x in items
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def normalize_public_channel_input(value):
    value = value.strip()
    if not value:
        return None

    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(
        r"^(?:www\.)?(?:t\.me|telegram\.me)/",
        "",
        value,
        flags=re.I,
    )
    value = value.strip().strip("/")
    value = value.split("?", 1)[0].split("#", 1)[0]

    if value.startswith("@"):
        value = value[1:]

    # Если дали ссылку на пост: t.me/channel/12345
    if "/" in value:
        value = value.split("/", 1)[0]

    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
        return None

    return value


async def resolve_public_channel(client, raw_value):
    username = normalize_public_channel_input(raw_value)
    if not username:
        raise ValueError(f"Не удалось распознать: {raw_value}")

    entity = await client.get_entity(username)
    if not isinstance(entity, Channel):
        raise ValueError(f"@{username} не является каналом/супергруппой.")

    return channel_item(entity)


def dedupe_channel_items(items):
    result = []
    seen = set()

    for item in items:
        channel_id = int(item["id"])
        if channel_id in seen:
            continue
        seen.add(channel_id)
        result.append(item)

    return result


def channel_limit_message(count):
    return (
        f"TelegramNewsAI поддерживает максимум {MAX_SELECTED_CHANNELS} "
        f"выбранных каналов/источников. Сейчас в списке: {int(count)}. "
        "Удалите лишние каналы командой R или создайте список заново командой N."
    )


def print_channel_limit_error(items):
    count = len(dedupe_channel_items(items))
    if count <= MAX_SELECTED_CHANNELS:
        return False
    print("\n" + channel_limit_message(count))
    print("Сетевой этап не запущен.")
    return True


def merge_resolved_channel(items, fresh_item):
    """Добавляет новый канал или освежает метаданные уже сохранённого."""
    channel_id = int(fresh_item["id"])

    for index, existing in enumerate(items):
        if int(existing["id"]) != channel_id:
            continue

        merged = dict(existing)
        changed = False

        for key in ("name", "username"):
            value = fresh_item.get(key)
            if value and merged.get(key) != value:
                merged[key] = value
                changed = True

        # entity нужен только в текущем процессе; save_selection его не пишет.
        if fresh_item.get("entity") is not None:
            merged["entity"] = fresh_item["entity"]

        items[index] = merged
        return items, False, changed

    items.append(fresh_item)
    return items, True, False


async def restore_saved_selection_from_session(client, items=None):
    """
    Восстанавливает сохранённые каналы строго из локального entity-cache.

    Используем client.session.get_input_entity, а не клиентский
    get_input_entity: последний при cache miss может сам обратиться к API.
    Полный список диалогов читается только как явный fallback, если
    локального access_hash для одного из сохранённых каналов не хватает.
    """
    saved_items = dedupe_channel_items(
        list(items) if items is not None else load_selection()
    )
    restored = []
    failed = []

    for saved in saved_items:
        sid = int(saved.get("id", 0))
        if sid <= 0:
            failed.append(saved)
            continue

        try:
            input_entity = client.session.get_input_entity(
                PeerChannel(sid)
            )
            restored.append({
                "id": sid,
                "name": (
                    saved.get("name")
                    or saved.get("username")
                    or str(sid)
                ),
                "username": saved.get("username"),
                "entity": input_entity,
            })
        except Exception:
            failed.append(saved)

    return dedupe_channel_items(restored), failed


async def restore_saved_selection(client, subscribed_by_id):
    restored = []
    failed = []

    for saved in load_selection():
        sid = int(saved.get("id", 0))

        if sid in subscribed_by_id:
            dialog = subscribed_by_id[sid]
            restored.append(
                channel_item(dialog.entity, dialog.name)
            )
            continue

        username = saved.get("username")
        if username:
            try:
                entity = await client.get_entity(username)
                if isinstance(entity, Channel):
                    restored.append(
                        channel_item(entity, saved.get("name"))
                    )
                    continue
            except Exception:
                pass

        failed.append(
            saved.get("name")
            or saved.get("username")
            or str(sid)
        )

    if failed:
        print("\nНе удалось восстановить некоторые сохраненные каналы:")
        for name in failed:
            print(f"  - {name}")

    return dedupe_channel_items(restored)


async def prompt_add_public_channels(client, items):
    items = dedupe_channel_items(items)
    if len(items) >= MAX_SELECTED_CHANNELS:
        print(
            "\nДостигнут продуктовый максимум: "
            f"{MAX_SELECTED_CHANNELS} выбранных каналов/источников. "
            "Сначала удалите лишний канал командой R."
        )
        return items

    print("\nМожно добавить публичные каналы БЕЗ подписки.")
    print("Поддерживаются:")
    print("  @example_channel")
    print("  https://t.me/example_channel")
    print("  https://t.me/example_channel/12345")
    print("Несколько адресов — через запятую.")
    print(
        "Если нужные подписанные каналы уже выбраны выше, "
        "повторять их номера здесь не нужно."
    )
    print("Enter или 0 — Готово, продолжить без добавления.")

    raw = input(
        "Введите публичный канал(ы) или нажмите Enter: "
    ).strip()

    if is_back_command(raw) or not raw:
        print("Дополнительные публичные каналы не добавлялись.")
        return items

    if looks_like_number_selection(raw):
        print(
            "Это похоже на номера из списка подписок. "
            "Выбранные там каналы уже добавлены."
        )
        print(
            "Здесь вводятся только @username или ссылки t.me. "
            "Продолжаю без дополнительных каналов."
        )
        return items

    added = 0
    updated = 0
    public_parts = [
        part.strip()
        for part in raw.split(",")
        if part.strip()
    ]

    for part_index, part in enumerate(public_parts, 1):
        part = part.strip()
        if not part:
            continue

        try:
            item = await resolve_public_channel(client, part)

            already_selected = any(
                int(existing["id"]) == int(item["id"])
                for existing in items
            )
            if (
                not already_selected
                and len(items) >= MAX_SELECTED_CHANNELS
            ):
                print(
                    f"  Не добавлен {part}: достигнут максимум "
                    f"{MAX_SELECTED_CHANNELS} источников."
                )
                continue

            items, was_added, was_updated = merge_resolved_channel(
                items,
                item,
            )

            uname = item.get("username") or "без username"
            if was_added:
                added += 1
                print(f"  Добавлен: {item['name']} (@{uname})")
            elif was_updated:
                updated += 1
                print(f"  Обновлён: {item['name']} (@{uname})")
            else:
                print(f"  Уже есть: {item['name']}")

        except Exception as e:
            print(f"  Не добавлен {part}: {e}")

        if part_index < len(public_parts):
            await asyncio.sleep(0.5)

    items = dedupe_channel_items(items)

    if added or updated:
        save_selection(items)
    if added:
        print(f"Добавлено новых каналов: {added}")
    if updated:
        print(f"Обновлено сохранённых каналов: {updated}")

    return items

def print_selected_channels(items):
    print("\nТекущий список каналов:")
    if not items:
        print("  (пусто)")
        return

    for i, item in enumerate(items, 1):
        username = item.get("username")
        suffix = f" @{username}" if username else ""
        print(f"{i:>4}. {item['name']}{suffix}")


def looks_like_number_selection(raw):
    compact = (raw or "").replace(" ", "")
    return bool(
        re.fullmatch(
            r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*",
            compact,
        )
    )


def parse_number_selection(raw, maximum):
    selected = set()
    raw = (raw or "").replace(" ", "")

    if not raw:
        return selected

    if maximum < 1:
        raise ValueError("Список каналов пуст.")

    for part in raw.split(","):
        if not part:
            raise ValueError("Обнаружен пустой номер.")

        if "-" in part:
            if part.count("-") != 1:
                raise ValueError(f"Некорректный диапазон: {part}")

            a_text, b_text = part.split("-", 1)
            if not a_text.isdigit() or not b_text.isdigit():
                raise ValueError(f"Некорректный диапазон: {part}")

            a, b = int(a_text), int(b_text)
            if not (1 <= a <= maximum and 1 <= b <= maximum):
                raise ValueError(
                    f"Диапазон {part} выходит за пределы списка 1-{maximum}."
                )

            selected.update(
                range(min(a, b), max(a, b) + 1)
            )
        else:
            if not part.isdigit():
                raise ValueError(f"Некорректный номер: {part}")

            n = int(part)
            if not 1 <= n <= maximum:
                raise ValueError(
                    f"Номер {n} вне списка 1-{maximum}."
                )
            selected.add(n)

    return selected

def parse_mixed_channel_selection(raw, maximum):
    """Разбирает одну строку с номерами/диапазонами и Telegram-адресами."""
    selected = set()
    public_values = []
    raw = str(raw or "").strip()

    if not raw:
        return selected, public_values

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Обнаружен пустой элемент выбора.")

        if part.casefold() == "all":
            if maximum < 1:
                raise ValueError("Список подписанных каналов пуст.")
            selected.update(range(1, maximum + 1))
            continue

        compact = part.replace(" ", "")
        if re.fullmatch(r"\d+(?:-\d+)?", compact):
            selected.update(
                parse_number_selection(compact, maximum)
            )
            continue

        if normalize_public_channel_input(part):
            public_values.append(part)
            continue

        raise ValueError(
            f"Не удалось распознать элемент: {part}"
        )

    return selected, unique_keep_order(public_values)


def prompt_remove_channels(items):
    if not items:
        print("\nСписок уже пуст.")
        return items

    print_selected_channels(items)
    print("\nВведите номера каналов, которые нужно удалить.")
    print("Пример: 2,5,8-10")
    print("0 — Назад (ничего не удалять).")

    raw = input(
        "Удалить: "
    ).strip()

    if is_back_command(raw):
        print("Удаление отменено.")
        return items

    if not raw:
        print("Ничего не выбрано. Список не изменён.")
        return items

    try:
        selected = parse_number_selection(
            raw,
            len(items),
        )
    except Exception:
        print("Не удалось разобрать номера. Ничего не удалено.")
        return items

    if not selected:
        print("Ничего не выбрано.")
        return items

    removed = [
        item
        for i, item in enumerate(items, 1)
        if i in selected
    ]

    kept = [
        item
        for i, item in enumerate(items, 1)
        if i not in selected
    ]

    for item in removed:
        print(f"  Удалён: {item['name']}")

    save_selection(kept)
    return kept


async def select_from_subscriptions(client, subscribed):
    print("\nВаши подписанные каналы:")

    for i, dialog in enumerate(subscribed, 1):
        username = getattr(
            dialog.entity,
            "username",
            None,
        )
        suffix = f" @{username}" if username else ""
        print(f"{i:>4}. {dialog.name}{suffix}")

    print("\nВведите номера через запятую; диапазоны — через дефис.")
    print("В этой же строке можно указывать @username и ссылки t.me.")
    print("Пример: 1,3,7-12,https://t.me/example_channel")
    print("Можно написать all или, например, all,@example_channel.")
    print("0 — Назад (сохранить прежний список).")

    while True:
        raw = input("Выбор: ").strip()

        if is_back_command(raw):
            print("Создание нового списка отменено.")
            return None

        try:
            selected_nums, public_values = parse_mixed_channel_selection(
                raw,
                len(subscribed),
            )
        except Exception as e:
            print(f"Не удалось разобрать выбор: {e}")
            print(
                "Введите номера, диапазоны, @username или ссылки t.me "
                "ещё раз; 0 — выход."
            )
            continue

        if not selected_nums and not public_values:
            print("Ни одного канала не выбрано.")
            print("Введите выбор ещё раз или 0 для выхода.")
            continue

        if len(selected_nums) > MAX_SELECTED_CHANNELS:
            print(
                f"Выбрано {len(selected_nums)} каналов, а TelegramNewsAI "
                f"поддерживает максимум {MAX_SELECTED_CHANNELS}. "
                "Уменьшите выбор."
            )
            continue

        items = [
            channel_item(
                subscribed[i - 1].entity,
                subscribed[i - 1].name,
            )
            for i in sorted(selected_nums)
        ]

        if public_values:
            print("\nДобавляю публичные каналы из этой же строки:")
            for public_index, raw_value in enumerate(public_values, 1):
                try:
                    item = await resolve_public_channel(
                        client,
                        raw_value,
                    )
                except Exception as e:
                    print(f"  Не добавлен {raw_value}: {e}")
                    if public_index < len(public_values):
                        await asyncio.sleep(0.5)
                    continue

                already_selected = any(
                    int(existing["id"]) == int(item["id"])
                    for existing in items
                )
                if (
                    not already_selected
                    and len(items) >= MAX_SELECTED_CHANNELS
                ):
                    print(
                        f"  Не добавлен {raw_value}: достигнут максимум "
                        f"{MAX_SELECTED_CHANNELS} источников."
                    )
                    if public_index < len(public_values):
                        await asyncio.sleep(0.5)
                    continue

                items, was_added, was_updated = merge_resolved_channel(
                    items,
                    item,
                )
                uname = item.get("username") or "без username"
                if was_added:
                    print(
                        f"  Добавлен: {item['name']} (@{uname})"
                    )
                elif was_updated:
                    print(
                        f"  Обновлён: {item['name']} (@{uname})"
                    )
                else:
                    print(f"  Уже есть: {item['name']}")

                if public_index < len(public_values):
                    await asyncio.sleep(0.5)

        items = dedupe_channel_items(items)
        if not items:
            print("Не удалось добавить ни одного канала.")
            print("Введите выбор ещё раз или 0 для выхода.")
            continue

        break

    print(
        f"\nВыбрано подписанных каналов: {len(selected_nums)}"
    )
    if selected_nums and len(selected_nums) <= 20:
        for i in sorted(selected_nums):
            item = channel_item(
                subscribed[i - 1].entity,
                subscribed[i - 1].name,
            )
            username = item.get("username")
            suffix = f" @{username}" if username else ""
            print(f"  + {item['name']}{suffix}")
    elif len(selected_nums) > 20:
        print(
            "Номера приняты. Список большой, "
            "поэтому названия не дублируются."
        )

    public_count = len(items) - len(selected_nums)
    if public_count > 0:
        print(
            f"Добавлено публичных каналов из строки: {public_count}"
        )

    print(
        "Эти каналы уже включены в новый список. "
        "Следующий шаг — только необязательное добавление "
        "ещё публичных каналов без подписки."
    )

    save_selection(items)

    items = await prompt_add_public_channels(
        client,
        items,
    )

    items = dedupe_channel_items(items)
    save_selection(items)
    return items


async def resolve_channels(
    client,
    initial_action=None,
    return_after_initial=False,
    skip_menu=False,
):
    """
    Получает сохранённый список каналов.

    skip_menu=True:
        обычный дайджест сразу использует сохранённый список без второго меню.

    initial_action=A/R/L/N:
        выполняет выбранную в главном меню операцию управления каналами.
    """
    pending_action = (
        str(initial_action).strip().lower()
        if initial_action
        else None
    )

    # A/R/L работают с уже сохранённым списком и не требуют полного
    # чтения подписок. A обращается к Telegram только за новым каналом.
    if pending_action in ("a", "r", "l"):
        restored = dedupe_channel_items(load_selection())

        if pending_action == "a":
            restored = await prompt_add_public_channels(client, restored)
        elif pending_action == "r":
            restored = prompt_remove_channels(restored)
        else:
            print_selected_channels(restored)

        save_selection(restored)
        return restored

    # Обычный дайджест/backfill сначала использует entity-cache уже
    # существующей Telegram-сессии. Это убирает get_dialogs(limit=None)
    # из штатного ежедневного пути и не меняет набор выбранных каналов.
    if skip_menu and not initial_action:
        saved = dedupe_channel_items(load_selection())
        if saved and print_channel_limit_error(saved):
            return []
        if saved:
            restored, cache_failed = (
                await restore_saved_selection_from_session(
                    client,
                    saved,
                )
            )
            if not cache_failed:
                return restored

            print(
                "\nЛокального кэша Telegram недостаточно для "
                f"{len(cache_failed)} канал(ов); выполняю разовое "
                "восстановление списка подписок."
            )

    print("\nПолучаю список ваших подписок...")
    dialogs = await client.get_dialogs(limit=None)

    subscribed = [d for d in dialogs if d.is_channel]
    subscribed_by_id = {
        int(d.entity.id): d
        for d in subscribed
    }

    restored = await restore_saved_selection(
        client,
        subscribed_by_id,
    )

    if (
        skip_menu
        and not initial_action
        and restored
        and print_channel_limit_error(restored)
    ):
        return []

    # Если сохранённого списка ещё нет, сначала создаём его.
    if not restored:
        new_selection = await select_from_subscriptions(
            client,
            subscribed,
        )

        if new_selection is None:
            return []

        restored = new_selection

        if not restored:
            return []

        # При обычном дайджесте сразу продолжаем с новым списком.
        if skip_menu and not initial_action:
            return restored

    # Обычный дайджест: никаких лишних меню.
    if skip_menu and not initial_action:
        return restored

    while True:
        if pending_action:
            ans = pending_action
            pending_action = None
        else:
            print(
                f"\nСохранённый список: "
                f"{len(restored)} каналов."
            )
            print("A     — добавить публичный канал(ы)")
            print("R     — удалить канал")
            print("L     — показать текущий список")
            print("N     — создать список заново")
            print("0     — назад")

            ans = input(
                "Ваш выбор [A/R/L/N/0]: "
            ).strip().lower()

        if is_back_command(ans) or ans in ("", "enter"):
            save_selection(restored)
            return restored

        if ans in ("a", "add", "д", "добавить"):
            restored = await prompt_add_public_channels(
                client,
                restored,
            )
            save_selection(restored)

            if return_after_initial and initial_action:
                return restored
            continue

        if ans in ("r", "remove", "у", "удалить"):
            restored = prompt_remove_channels(
                restored
            )
            save_selection(restored)

            if return_after_initial and initial_action:
                return restored
            continue

        if ans in ("l", "list", "с", "список"):
            print_selected_channels(restored)

            if return_after_initial and initial_action:
                return restored
            continue

        if ans in ("n", "new", "н", "заново"):
            new_selection = await select_from_subscriptions(
                client,
                subscribed,
            )

            if new_selection is None:
                if return_after_initial and initial_action:
                    return restored
                continue

            restored = new_selection
            save_selection(restored)

            if return_after_initial and initial_action:
                return restored
            continue

        print("Неизвестная команда.")


# ============================================================
# Text cleanup
# ============================================================

PROMO_LINE_PATTERNS = [
    r"^\s*➡️?\s*подписаться\s*$",
    r"^\s*👉?\s*підписатися\s*$",
    r"^\s*підписуйся на канал\s*$",
    r"^\s*подпишись на канал\s*$",
    r"^\s*надіслати новину\s+@\S+\s*$",
    r"^\s*прислать новость\s+@\S+\s*$",
]


def strip_promo_lines(text):
    lines = []

    for line in (text or "").splitlines():
        stripped = line.strip()

        if not stripped:
            lines.append("")
            continue

        matched = any(
            re.match(pattern, stripped, flags=re.I)
            for pattern in PROMO_LINE_PATTERNS
        )

        if not matched:
            lines.append(line)

    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(
        r"[^\w\s]+",
        " ",
        text,
        flags=re.UNICODE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_routine_alert(text):
    """Legacy hook kept for compatibility without topic-specific rules."""
    return False


# ============================================================
# Message metadata
# ============================================================

URL_RE = re.compile(
    r"https?://[^\s<>\[\]{}]+",
    flags=re.I,
)


def public_channel_link(username):
    if not isinstance(username, str):
        return None
    username = username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return None
    return f"https://t.me/{username}"


def public_link(username, message_id):
    channel_url = public_channel_link(username)
    if channel_url and isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
        return f"{channel_url}/{message_id}"
    return None


def enrich_forward_info_links(info):
    """Добавляет проверяемые ссылки только для публичного источника пересылки."""
    if not isinstance(info, dict):
        return info

    result = dict(info)
    username = result.get("chat_username")
    if not username:
        return result

    channel_url = public_channel_link(username)
    if channel_url:
        result["channel_url"] = channel_url

    channel_post = result.get("channel_post")
    telegram_url = public_link(username, channel_post)
    if telegram_url:
        result["telegram_url"] = telegram_url

    return result


def reaction_to_label(reaction):
    if reaction is None:
        return None

    emoticon = getattr(reaction, "emoticon", None)
    if emoticon:
        return emoticon

    document_id = getattr(reaction, "document_id", None)
    if document_id is not None:
        return f"custom_emoji:{document_id}"

    return reaction.__class__.__name__


def extract_reactions(msg):
    reactions = getattr(msg, "reactions", None)
    results = getattr(reactions, "results", None)

    if not results:
        return []

    output = []

    for item in results:
        output.append({
            "reaction": reaction_to_label(
                getattr(item, "reaction", None)
            ),
            "count": getattr(item, "count", None),
            "chosen_order": getattr(
                item,
                "chosen_order",
                None,
            ),
        })

    return output


def extract_reply_count(msg):
    replies = getattr(msg, "replies", None)
    if replies is None:
        return None
    return getattr(replies, "replies", None)


def extract_media_info(msg):
    media = getattr(msg, "media", None)
    if media is None:
        return {
            "type": None,
            "mime_type": None,
            "file_name": None,
            "identity": None,
        }

    media_type = None
    media_identity = None

    try:
        photo = getattr(msg, "photo", None)
        document = getattr(msg, "document", None)

        if photo:
            media_type = "photo"
            photo_id = getattr(photo, "id", None)
            if photo_id is not None:
                media_identity = f"photo:{int(photo_id)}"
        elif getattr(msg, "video", None):
            media_type = "video"
        elif getattr(msg, "voice", None):
            media_type = "voice"
        elif getattr(msg, "audio", None):
            media_type = "audio"
        elif getattr(msg, "gif", None):
            media_type = "gif"
        elif getattr(msg, "sticker", None):
            media_type = "sticker"
        elif getattr(msg, "poll", None):
            media_type = "poll"
        elif getattr(msg, "geo", None):
            media_type = "geo"
        elif getattr(msg, "venue", None):
            media_type = "venue"
        else:
            class_name = media.__class__.__name__
            if class_name == "MessageMediaWebPage":
                media_type = "web_preview"
            else:
                media_type = class_name

        # Video/audio/voice/GIF/sticker Telegram хранит как Document.
        # ID нужен не для скачивания, а чтобы дедупликатор не считал две
        # разные картинки/ролика с одинаковой подписью одним сообщением.
        if media_identity is None and document is not None:
            document_id = getattr(document, "id", None)
            if document_id is not None:
                media_identity = f"document:{int(document_id)}"

        if media_identity is None and media_type == "poll":
            poll_obj = getattr(media, "poll", None)
            poll_id = getattr(poll_obj, "id", None)
            if poll_id is not None:
                media_identity = f"poll:{int(poll_id)}"

        if media_identity is None and media_type == "web_preview":
            webpage = getattr(media, "webpage", None)
            webpage_id = getattr(webpage, "id", None)
            if webpage_id is not None:
                media_identity = f"webpage:{int(webpage_id)}"

        if media_identity is None and media_type in ("geo", "venue"):
            geo = getattr(msg, "geo", None)
            lat = getattr(geo, "lat", None)
            lon = getattr(geo, "long", None)
            if lat is not None and lon is not None:
                media_identity = f"geo:{float(lat):.7f},{float(lon):.7f}"

    except Exception:
        media_type = media.__class__.__name__
        media_identity = None

    file_obj = getattr(msg, "file", None)

    return {
        "type": media_type,
        "mime_type": getattr(
            file_obj,
            "mime_type",
            None,
        ),
        "file_name": getattr(
            file_obj,
            "name",
            None,
        ),
        "identity": media_identity,
    }


def peer_to_dict(peer):
    if peer is None:
        return None

    for attr, kind in (
        ("channel_id", "channel"),
        ("chat_id", "chat"),
        ("user_id", "user"),
    ):
        value = getattr(peer, attr, None)
        if value is not None:
            return {
                "type": kind,
                "id": int(value),
            }

    return {
        "type": peer.__class__.__name__,
    }


def extract_forward_info(msg):
    raw = getattr(msg, "fwd_from", None)
    if raw is None:
        return None

    result = {
        "date_utc": iso_utc(
            getattr(raw, "date", None)
        ),
        "from_id": peer_to_dict(
            getattr(raw, "from_id", None)
        ),
        "from_name": getattr(
            raw,
            "from_name",
            None,
        ),
        "channel_post": getattr(
            raw,
            "channel_post",
            None,
        ),
        "post_author": getattr(
            raw,
            "post_author",
            None,
        ),
    }

    try:
        forward = getattr(msg, "forward", None)

        if forward is not None:
            chat = getattr(forward, "chat", None)
            sender = getattr(forward, "sender", None)

            if chat is not None:
                result["chat_title"] = (
                    getattr(chat, "title", None)
                    or getattr(chat, "username", None)
                )
                result["chat_username"] = getattr(
                    chat,
                    "username",
                    None,
                )

            if sender is not None:
                first_name = getattr(
                    sender,
                    "first_name",
                    None,
                )
                last_name = getattr(
                    sender,
                    "last_name",
                    None,
                )
                sender_name = " ".join(
                    x for x in (
                        first_name,
                        last_name,
                    )
                    if x
                ).strip()

                result["sender_name"] = (
                    sender_name or None
                )
                result["sender_username"] = getattr(
                    sender,
                    "username",
                    None,
                )

    except Exception:
        pass

    cleaned = {
        k: v
        for k, v in result.items()
        if v is not None
    }
    return enrich_forward_info_links(cleaned) or None


def extract_external_urls(msg):
    urls = []
    text = getattr(msg, "message", None) or ""

    # Видимые URL в тексте.
    for match in URL_RE.findall(text):
        cleaned = match.rstrip(
            ".,;:!?)]}\"'»"
        )
        urls.append(cleaned)

    # Скрытые ссылки в Telegram-разметке.
    try:
        for entity, inner_text in msg.get_entities_text():
            class_name = entity.__class__.__name__

            if class_name == "MessageEntityTextUrl":
                url = getattr(entity, "url", None)
                if url:
                    urls.append(url)

            elif class_name == "MessageEntityUrl":
                if inner_text:
                    urls.append(inner_text)

            elif class_name == "MessageEntityEmail":
                if inner_text:
                    urls.append(
                        "mailto:" + inner_text
                    )
    except Exception:
        pass

    # URL-кнопки.
    try:
        buttons = getattr(msg, "buttons", None)
        if buttons:
            for row in buttons:
                for button in row:
                    url = getattr(button, "url", None)

                    if not url:
                        raw_button = getattr(
                            button,
                            "button",
                            None,
                        )
                        url = getattr(
                            raw_button,
                            "url",
                            None,
                        )

                    if url:
                        urls.append(url)
    except Exception:
        pass

    # URL из web preview.
    try:
        media = getattr(msg, "media", None)
        webpage = getattr(media, "webpage", None)

        if webpage is not None:
            url = getattr(webpage, "url", None)
            if url:
                urls.append(url)
    except Exception:
        pass

    return unique_keep_order(urls)


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "yclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "vero_conv",
    "vero_id",
    "ref_src",
    "ref_url",
}


def normalize_external_url(url):
    """
    Канонизирует ссылку для сравнения источников:
    - lower-case host;
    - убирает стандартный порт;
    - удаляет UTM и распространённые tracking-параметры;
    - сортирует оставшиеся query-параметры;
    - убирает fragment и лишний завершающий slash.
    Исходная ссылка при этом сохраняется отдельно.
    """
    if not url:
        return None

    url = str(url).strip()
    if not url:
        return None

    if url.lower().startswith("mailto:"):
        return url.lower()

    try:
        parts = urlsplit(url)

        if not parts.scheme:
            parts = urlsplit("https://" + url)

        scheme = (parts.scheme or "https").lower()
        host = (parts.hostname or "").lower()

        if not host:
            return url

        port = parts.port
        if port and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"
        else:
            netloc = host

        # www обычно не меняет ресурс и мешает группировке.
        if netloc.startswith("www."):
            netloc = netloc[4:]

        path = re.sub(r"/{2,}", "/", parts.path or "/")
        if path != "/":
            path = path.rstrip("/")
        else:
            path = ""

        cleaned_query = []

        for key, value in parse_qsl(
            parts.query,
            keep_blank_values=True,
        ):
            key_lower = key.lower()

            if (
                key_lower.startswith("utm_")
                or key_lower in TRACKING_QUERY_KEYS
            ):
                continue

            cleaned_query.append((key, value))

        cleaned_query.sort(
            key=lambda item: (
                item[0].lower(),
                item[1],
            )
        )

        query = urlencode(
            cleaned_query,
            doseq=True,
        )

        return urlunsplit(
            (
                scheme,
                netloc,
                path,
                query,
                "",
            )
        )

    except Exception:
        return url


def normalize_external_urls(urls):
    return unique_keep_order(
        normalize_external_url(url)
        for url in (urls or [])
        if url
    )


GENERIC_SHARED_SOURCE_ROUTES = {
    "about",
    "author",
    "authors",
    "bot",
    "bots",
    "category",
    "categories",
    "contact",
    "contacts",
    "download",
    "downloads",
    "feed",
    "join",
    "login",
    "privacy",
    "profile",
    "profiles",
    "promo",
    "redirect",
    "register",
    "rss",
    "search",
    "signin",
    "signup",
    "subscribe",
    "subscription",
    "subscriptions",
    "tag",
    "tags",
    "terms",
}


def _looks_like_specific_material_segment(segment):
    value = str(segment or "").strip().casefold()
    if not value or value in GENERIC_SHARED_SOURCE_ROUTES:
        return False

    # Для related groups нужен консервативный признак конкретного материала,
    # а не просто любой path/query. Даты, идентификаторы и длинные slug-и
    # обычно дают такой признак без привязки к конкретному сайту.
    if any(char.isdigit() for char in value):
        return True
    if ("-" in value or "_" in value) and len(value) >= 8:
        return True
    if len(value) >= 18 and value.isalnum():
        return True
    return False


def is_specific_shared_source_url(url):
    """
    True только для достаточно конкретной публикации/документа.

    Related groups — подсказка, поэтому лучше пропустить слабую связь, чем
    объединить разные события постоянной promo/profile/service URL.
    """
    if not isinstance(url, str):
        return False

    value = url.strip()
    if not value or value.lower().startswith("mailto:"):
        return False

    try:
        parts = urlsplit(value)
    except Exception:
        return False

    host = (parts.hostname or "").lower()
    if not host:
        return False

    path = (parts.path or "").strip("/")
    segments = [segment for segment in path.split("/") if segment]

    if host in {"t.me", "telegram.me"}:
        return len(segments) >= 2 and segments[-1].isdigit()

    # У MAX односегментный путь max.ru/<name> — профиль/канал.
    if host == "max.ru":
        if len(segments) < 2:
            return False
        if segments[0].casefold() in {"join", "joinchannel"}:
            return False
        return _looks_like_specific_material_segment(segments[-1])

    # Query сам по себе не делает ссылку конкретным материалом:
    # именно так постоянные app/deep-link redirect URL связывали десятки
    # разных новостей в RUN 1.
    if not segments:
        return False

    if segments[0].casefold() in GENERIC_SHARED_SOURCE_ROUTES:
        return False

    return any(
        _looks_like_specific_material_segment(segment)
        for segment in segments[-2:]
    )


def make_origin_key(forwarded_from, canonical_urls):
    if forwarded_from:
        from_id = forwarded_from.get("from_id")
        channel_post = forwarded_from.get("channel_post")

        if (
            isinstance(from_id, dict)
            and from_id.get("type") == "channel"
            and from_id.get("id") is not None
            and channel_post is not None
        ):
            return (
                f"telegram_forward:"
                f"{from_id['id']}:"
                f"{channel_post}"
            )

    for url in canonical_urls or []:
        if is_specific_shared_source_url(url):
            return "url:" + url

    return None


def make_message(channel, msg, settings):
    raw_text = (
        getattr(msg, "message", None)
        or ""
    ).strip()

    media_info = extract_media_info(msg)

    if not raw_text:
        if not settings.get(
            "include_media_only_messages",
            True,
        ):
            return None

        if not media_info.get("type"):
            return None

        raw_text = (
            "[Медиа без подписи: "
            f"{media_info['type']}]"
        )

    text = raw_text

    if settings.get(
        "strip_promo_lines",
        True,
    ):
        text = strip_promo_lines(text)

    if (
        not text
        and not media_info.get("type")
    ):
        return None

    username = channel.get("username")
    external_urls = extract_external_urls(msg)
    canonical_urls = normalize_external_urls(
        external_urls
    )
    forwarded_from = extract_forward_info(msg)
    reactions = extract_reactions(msg)

    edit_date = getattr(
        msg,
        "edit_date",
        None,
    )

    reply_to = getattr(
        msg,
        "reply_to_msg_id",
        None,
    )

    if reply_to is None:
        reply_header = getattr(
            msg,
            "reply_to",
            None,
        )
        reply_to = getattr(
            reply_header,
            "reply_to_msg_id",
            None,
        )

    return {
        "channel_id": int(channel["id"]),
        "channel": channel["name"],
        "username": username,
        "channel_url": public_channel_link(username),

        "message_id": int(msg.id),

        "date_utc": iso_utc(
            getattr(msg, "date", None)
        ),
        "date_local": iso_local(
            getattr(msg, "date", None)
        ),

        "edit_date_utc": iso_utc(edit_date),
        "edit_date_local": iso_local(edit_date),
        "edited": bool(edit_date),

        "telegram_url": public_link(
            username,
            int(msg.id),
        ),

        "text": text,
        "raw_text": (getattr(msg, "message", None) or ""),
        "raw_text_available": True,

        "views": getattr(msg, "views", None),
        "forwards": getattr(
            msg,
            "forwards",
            None,
        ),
        "replies": extract_reply_count(msg),
        "reactions": reactions,

        "media": media_info,
        "album_id": (
            str(getattr(msg, "grouped_id", ""))
            if getattr(msg, "grouped_id", None)
            is not None
            else None
        ),

        "reply_to_message_id": reply_to,
        "post_author": getattr(
            msg,
            "post_author",
            None,
        ),

        "forwarded_from": forwarded_from,
        "external_urls": external_urls,
        "canonical_urls": canonical_urls,
        "origin_key": make_origin_key(
            forwarded_from,
            canonical_urls,
        ),

        "duplicates": [],
    }


def semantic_content_hash(message):
    """
    Хэш только смыслового содержимого.
    Просмотры, реакции, forwards и replies сюда НЕ входят.
    """
    media = copy.deepcopy(message.get("media"))
    if isinstance(media, dict):
        # identity нужен только для безопасной дедупликации. Его появление
        # после обновления версии не является содержательной правкой поста.
        media.pop("identity", None)

    forwarded_from = copy.deepcopy(
        message.get("forwarded_from")
    )
    if isinstance(forwarded_from, dict):
        # Эти URL вычисляются из chat_username/channel_post и сами по себе
        # не означают изменение исходной Telegram-публикации.
        forwarded_from.pop("channel_url", None)
        forwarded_from.pop("telegram_url", None)

    relevant = {
        "text": message.get("text"),
        "media": media,
        "album_id": message.get("album_id"),
        "reply_to_message_id": message.get(
            "reply_to_message_id"
        ),
        "post_author": message.get("post_author"),
        "canonical_urls": message.get(
            "canonical_urls",
            [],
        ),
        "forwarded_from": forwarded_from,
    }

    raw = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(raw).hexdigest()


def metrics_hash(message):
    """
    Хэш динамических счётчиков. Его изменение не считается
    изменением новости.
    """
    relevant = {
        "views": message.get("views"),
        "forwards": message.get("forwards"),
        "replies": message.get("replies"),
        "reactions": message.get(
            "reactions"
        ),
    }

    raw = json.dumps(
        relevant,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(raw).hexdigest()


def message_key(message):
    return (
        f"{int(message['channel_id'])}:"
        f"{int(message['message_id'])}"
    )


# ============================================================
# SQLite
# ============================================================

def table_columns(conn, table_name):
    return {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def ensure_column(
    conn,
    table_name,
    column_name,
    definition,
):
    columns = table_columns(
        conn,
        table_name,
    )

    if column_name in columns:
        return False

    conn.execute(
        f"ALTER TABLE {table_name} "
        f"ADD COLUMN {column_name} {definition}"
    )
    return True


SEARCH_INDEX_VERSION = "2"


def ensure_search_index(conn):
    """
    Создаёт FTS5-индекс поверх messages. Если конкретная сборка SQLite
    не содержит FTS5, программа продолжает работать и поиск использует LIKE.
    """
    result = {
        "fts5_available": False,
        "rebuilt": False,
        "error": None,
    }

    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(
                text,
                channel_name,
                username,
                content='messages',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            )
        """)

        conn.execute('DROP TRIGGER IF EXISTS messages_fts_ai')
        conn.execute('DROP TRIGGER IF EXISTS messages_fts_ad')
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_ai
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(
                    rowid, text, channel_name, username
                ) VALUES (
                    new.rowid, replace(replace(new.text,'ё','е'),'Ё','Е'), new.channel_name, new.username
                );
            END
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_ad
            AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(
                    messages_fts, rowid, text, channel_name, username
                ) VALUES (
                    'delete', old.rowid, replace(replace(old.text,'ё','е'),'Ё','Е'), old.channel_name, old.username
                );
            END
        """)

        conn.execute("DROP TRIGGER IF EXISTS messages_fts_au")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_fts_au
            AFTER UPDATE OF text, channel_name, username ON messages
            WHEN old.text IS NOT new.text OR old.channel_name IS NOT new.channel_name
              OR old.username IS NOT new.username BEGIN
                INSERT INTO messages_fts(
                    messages_fts, rowid, text, channel_name, username
                ) VALUES (
                    'delete', old.rowid, replace(replace(old.text,'ё','е'),'Ё','Е'), old.channel_name, old.username
                );
                INSERT INTO messages_fts(
                    rowid, text, channel_name, username
                ) VALUES (
                    new.rowid, replace(replace(new.text,'ё','е'),'Ё','Е'), new.channel_name, new.username
                );
            END
        """)

        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            ("fts_index_version",),
        ).fetchone()

        current_version = row["value"] if row else None

        if current_version != SEARCH_INDEX_VERSION:
            conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('delete-all')"
            )
            conn.execute("INSERT INTO messages_fts(rowid,text,channel_name,username) SELECT rowid,replace(replace(text,'ё','е'),'Ё','Е'),channel_name,username FROM messages")
            conn.execute(
                """
                INSERT INTO app_meta(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("fts_index_version", SEARCH_INDEX_VERSION),
            )
            result["rebuilt"] = True

        conn.commit()
        result["fts5_available"] = True
        return result

    except Exception as e:
        conn.rollback()
        result["error"] = f"{type(e).__name__}: {e}"
        return result


def _v4_open_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            last_message_id INTEGER DEFAULT 0,
            last_sync_utc TEXT,
            last_success_utc TEXT,
            last_error TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,

            channel_name TEXT,
            username TEXT,

            date_utc TEXT,
            date_local TEXT,
            edit_date_utc TEXT,
            edit_date_local TEXT,

            telegram_url TEXT,
            text TEXT,

            views INTEGER,
            forwards INTEGER,
            replies INTEGER,

            reactions_json TEXT,
            media_json TEXT,
            album_id TEXT,
            reply_to_message_id INTEGER,
            post_author TEXT,

            forwarded_from_json TEXT,
            external_urls_json TEXT,
            canonical_urls_json TEXT,
            origin_key TEXT,

            first_seen_utc TEXT,
            last_seen_utc TEXT,

            content_hash TEXT,
            metrics_hash TEXT,
            hash_version INTEGER DEFAULT 4,
            content_changed_utc TEXT,
            metrics_changed_utc TEXT,

            PRIMARY KEY (
                channel_id,
                message_id
            )
        )
    """)

    # Миграция существующей news.db со старых версий без потери истории.
    ensure_column(
        conn,
        "messages",
        "canonical_urls_json",
        "TEXT",
    )
    ensure_column(
        conn,
        "messages",
        "metrics_hash",
        "TEXT",
    )
    ensure_column(
        conn,
        "messages",
        "hash_version",
        "INTEGER DEFAULT 0",
    )
    ensure_column(
        conn,
        "messages",
        "content_changed_utc",
        "TEXT",
    )
    ensure_column(
        conn,
        "messages",
        "metrics_changed_utc",
        "TEXT",
    )

    ensure_column(
        conn,
        "channels",
        "history_coverage_utc",
        "TEXT",
    )
    ensure_column(
        conn,
        "channels",
        "history_backfill_updated_utc",
        "TEXT",
    )

    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_messages_date
        ON messages(date_utc)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_messages_origin
        ON messages(origin_key)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL,
            created_local TEXT NOT NULL,
            hours REAL NOT NULL,
            messages_exported INTEGER NOT NULL,
            successful_channels INTEGER NOT NULL,
            failed_channels INTEGER NOT NULL,
            latest_file TEXT,
            selection_fingerprint TEXT
        )
    """)

    # Старые записи runs не имели привязки к набору каналов и поэтому
    # не могут безопасно использоваться как точка сравнения.
    ensure_column(
        conn,
        "runs",
        "selection_fingerprint",
        "TEXT",
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_runs_selection_fingerprint
        ON runs(selection_fingerprint, id)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()

    fts_status = ensure_search_index(conn)
    try:
        conn.execute(
            """
            INSERT INTO app_meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                "fts5_available",
                "1" if fts_status.get("fts5_available") else "0",
            ),
        )
        if fts_status.get("error"):
            conn.execute(
                """
                INSERT INTO app_meta(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("fts5_error", fts_status.get("error")),
            )
        conn.commit()
    except Exception:
        conn.rollback()

    return conn


def selection_fingerprint(channels):
    """Stable identity of an exact channel set; order and display names do not matter."""
    channel_ids = sorted({
        int(channel["id"])
        for channel in channels
    })
    raw = json.dumps(
        channel_ids,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _digest_reference_from_file(path, expected_fingerprint):
    """
    Возвращает время только для JSON того же набора каналов.
    Старые файлы без selection_fingerprint намеренно не используются:
    безопаснее один раз начать новое сравнение, чем смешать разные ленты.
    """
    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8")
        )
        meta = payload.get("meta") or {}

        if meta.get("selection_fingerprint") != expected_fingerprint:
            return None

        for field in ("created_utc", "created_local"):
            dt = parse_dt(meta.get(field))
            if dt:
                return iso_utc(dt)

    except Exception:
        return None

    return None


def get_previous_digest_reference(conn, channels):
    """
    Ищет предыдущий дайджест только для точного текущего набора каналов.

    Старые runs/JSON без selection_fingerprint не считаются сопоставимыми.
    Если пользователь вернётся к ранее использованному набору каналов,
    будет найдена последняя точка сравнения именно этого набора.
    """
    fingerprint = selection_fingerprint(channels)

    row = conn.execute(
        """
        SELECT created_utc
        FROM runs
        WHERE selection_fingerprint = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()

    if row and row["created_utc"]:
        return row["created_utc"], "news.db:runs"

    candidates = []

    if LATEST_FILE.exists():
        candidates.append(
            (LATEST_FILE, "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.json")
        )

    if ARCHIVE_DIR.exists():
        try:
            # Папка содержит только датированные JSON-архивы дайджестов.
            # Общий шаблон сохраняет чтение файлов, созданных старыми версиями,
            # без переноса их исторического имени в новый экспортный контракт.
            archives = sorted(
                ARCHIVE_DIR.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(
                (path, f"archive:{path.name}")
                for path in archives
            )
        except Exception:
            pass

    for candidate, source in candidates:
        created_utc = _digest_reference_from_file(
            candidate,
            fingerprint,
        )
        if created_utc:
            return created_utc, source

    return None, None


def get_channel_state(conn, channel_id):
    row = conn.execute(
        """
        SELECT *
        FROM channels
        WHERE channel_id = ?
        """,
        (int(channel_id),),
    ).fetchone()

    return dict(row) if row else None


def upsert_channel_state(
    conn,
    channel,
    last_message_id=None,
    success=False,
    error=None,
):
    now = iso_utc(utc_now())
    existing = get_channel_state(
        conn,
        channel["id"],
    )

    current_last_id = (
        int(existing["last_message_id"])
        if existing
        and existing.get("last_message_id")
        is not None
        else 0
    )

    if last_message_id is None:
        last_message_id = current_last_id

    last_message_id = max(
        current_last_id,
        int(last_message_id or 0),
    )

    last_success = (
        now
        if success
        else (
            existing.get("last_success_utc")
            if existing
            else None
        )
    )

    conn.execute(
        """
        INSERT INTO channels (
            channel_id,
            name,
            username,
            last_message_id,
            last_sync_utc,
            last_success_utc,
            last_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id)
        DO UPDATE SET
            name = excluded.name,
            username = excluded.username,
            last_message_id = excluded.last_message_id,
            last_sync_utc = excluded.last_sync_utc,
            last_success_utc = excluded.last_success_utc,
            last_error = excluded.last_error
        """,
        (
            int(channel["id"]),
            channel["name"],
            channel.get("username"),
            last_message_id,
            now,
            last_success,
            error,
        ),
    )


def _v4_upsert_message(conn, message):
    now = iso_utc(utc_now())

    existing = conn.execute(
        """
        SELECT
            first_seen_utc,
            text,
            views,
            forwards,
            replies,
            reactions_json,
            media_json,
            album_id,
            reply_to_message_id,
            post_author,
            forwarded_from_json,
            external_urls_json,
            content_hash,
            metrics_hash,
            hash_version,
            content_changed_utc,
            metrics_changed_utc
        FROM messages
        WHERE
            channel_id = ?
            AND message_id = ?
        """,
        (
            int(message["channel_id"]),
            int(message["message_id"]),
        ),
    ).fetchone()

    semantic_digest = semantic_content_hash(
        message
    )
    metric_digest = metrics_hash(message)

    content_changed_utc = None
    metrics_changed_utc = None

    if existing is None:
        status = "new"
        first_seen = now
        hash_version = 4

    else:
        first_seen = existing["first_seen_utc"]
        old_hash_version = int(
            existing["hash_version"] or 0
        )

        # Первая встреча записи после обновления с 3.x.
        # Старый content_hash включал метрики, поэтому пересчитываем
        # старое содержимое прямо из сохранённых колонок БД.
        # Так даже первый запуск 4.0 не теряет реальную правку поста.
        if old_hash_version < 4:
            old_message = {
                "text": existing["text"],
                "views": existing["views"],
                "forwards": existing["forwards"],
                "replies": existing["replies"],
                "reactions": json_loads(
                    existing["reactions_json"],
                    [],
                ),
                "media": json_loads(
                    existing["media_json"],
                    {},
                ),
                "album_id": existing[
                    "album_id"
                ],
                "reply_to_message_id": existing[
                    "reply_to_message_id"
                ],
                "post_author": existing[
                    "post_author"
                ],
                "forwarded_from": json_loads(
                    existing[
                        "forwarded_from_json"
                    ],
                    None,
                ),
                "canonical_urls": normalize_external_urls(
                    json_loads(
                        existing[
                            "external_urls_json"
                        ],
                        [],
                    )
                ),
            }

            old_semantic_digest = (
                semantic_content_hash(
                    old_message
                )
            )
            old_metric_digest = (
                metrics_hash(
                    old_message
                )
            )

            content_changed = (
                old_semantic_digest
                != semantic_digest
            )
            metrics_changed = (
                old_metric_digest
                != metric_digest
            )

            content_changed_utc = (
                now
                if content_changed
                else existing[
                    "content_changed_utc"
                ]
            )
            metrics_changed_utc = (
                now
                if metrics_changed
                else existing[
                    "metrics_changed_utc"
                ]
            )

            hash_version = 4

            if content_changed and metrics_changed:
                status = "content_and_metrics_changed"
            elif content_changed:
                status = "content_changed"
            elif metrics_changed:
                status = "metrics_changed"
            else:
                status = "migrated"

        else:
            content_changed = (
                existing["content_hash"]
                != semantic_digest
            )
            metrics_changed = (
                existing["metrics_hash"]
                != metric_digest
            )

            content_changed_utc = (
                now
                if content_changed
                else existing[
                    "content_changed_utc"
                ]
            )

            metrics_changed_utc = (
                now
                if metrics_changed
                else existing[
                    "metrics_changed_utc"
                ]
            )

            hash_version = 4

            if content_changed and metrics_changed:
                status = "content_and_metrics_changed"
            elif content_changed:
                status = "content_changed"
            elif metrics_changed:
                status = "metrics_changed"
            else:
                status = "same"

    conn.execute(
        """
        INSERT INTO messages (
            channel_id,
            message_id,
            channel_name,
            username,
            date_utc,
            date_local,
            edit_date_utc,
            edit_date_local,
            telegram_url,
            text,
            views,
            forwards,
            replies,
            reactions_json,
            media_json,
            album_id,
            reply_to_message_id,
            post_author,
            forwarded_from_json,
            external_urls_json,
            canonical_urls_json,
            origin_key,
            first_seen_utc,
            last_seen_utc,
            content_hash,
            metrics_hash,
            hash_version,
            content_changed_utc,
            metrics_changed_utc
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(channel_id, message_id)
        DO UPDATE SET
            channel_name = excluded.channel_name,
            username = excluded.username,
            date_utc = excluded.date_utc,
            date_local = excluded.date_local,
            edit_date_utc = excluded.edit_date_utc,
            edit_date_local = excluded.edit_date_local,
            telegram_url = excluded.telegram_url,
            text = excluded.text,
            views = excluded.views,
            forwards = excluded.forwards,
            replies = excluded.replies,
            reactions_json = excluded.reactions_json,
            media_json = excluded.media_json,
            album_id = excluded.album_id,
            reply_to_message_id = excluded.reply_to_message_id,
            post_author = excluded.post_author,
            forwarded_from_json = excluded.forwarded_from_json,
            external_urls_json = excluded.external_urls_json,
            canonical_urls_json = excluded.canonical_urls_json,
            origin_key = excluded.origin_key,
            first_seen_utc = messages.first_seen_utc,
            last_seen_utc = excluded.last_seen_utc,
            content_hash = excluded.content_hash,
            metrics_hash = excluded.metrics_hash,
            hash_version = excluded.hash_version,
            content_changed_utc = excluded.content_changed_utc,
            metrics_changed_utc = excluded.metrics_changed_utc
        """,
        (
            int(message["channel_id"]),
            int(message["message_id"]),
            message.get("channel"),
            message.get("username"),
            message.get("date_utc"),
            message.get("date_local"),
            message.get("edit_date_utc"),
            message.get("edit_date_local"),
            message.get("telegram_url"),
            message.get("text"),
            message.get("views"),
            message.get("forwards"),
            message.get("replies"),
            json_dumps(
                message.get("reactions", [])
            ),
            json_dumps(
                message.get("media", {})
            ),
            message.get("album_id"),
            message.get(
                "reply_to_message_id"
            ),
            message.get("post_author"),
            json_dumps(
                message.get("forwarded_from")
            ),
            json_dumps(
                message.get(
                    "external_urls",
                    [],
                )
            ),
            json_dumps(
                message.get(
                    "canonical_urls",
                    [],
                )
            ),
            message.get("origin_key"),
            first_seen,
            now,
            semantic_digest,
            metric_digest,
            hash_version,
            content_changed_utc,
            metrics_changed_utc,
        ),
    )

    return status


def _v4_db_row_to_message(row, previous_run_utc):
    canonical_urls = json_loads(
        row["canonical_urls_json"],
        [],
    )

    # Для старых строк, которые ещё не обновлялись Telegram после миграции.
    if not canonical_urls:
        canonical_urls = normalize_external_urls(
            json_loads(
                row["external_urls_json"],
                [],
            )
        )

    message = {
        "channel_id": row["channel_id"],
        "message_id": row["message_id"],
        "message_key": (
            f"{row['channel_id']}:"
            f"{row['message_id']}"
        ),

        "channel": row["channel_name"],
        "username": row["username"],

        "date_utc": row["date_utc"],
        "date_local": row["date_local"],

        "edit_date_utc": row[
            "edit_date_utc"
        ],
        "edit_date_local": row[
            "edit_date_local"
        ],
        "edited": bool(
            row["edit_date_utc"]
        ),

        "telegram_url": row[
            "telegram_url"
        ],
        "text": row["text"],

        "views": row["views"],
        "forwards": row["forwards"],
        "replies": row["replies"],

        "reactions": json_loads(
            row["reactions_json"],
            [],
        ),
        "media": json_loads(
            row["media_json"],
            {},
        ),

        "album_id": row["album_id"],
        "reply_to_message_id": row[
            "reply_to_message_id"
        ],
        "post_author": row[
            "post_author"
        ],

        "forwarded_from": enrich_forward_info_links(
            json_loads(
                row["forwarded_from_json"],
                None,
            )
        ),
        "external_urls": json_loads(
            row["external_urls_json"],
            [],
        ),
        "canonical_urls": canonical_urls,
        "origin_key": row["origin_key"],

        "duplicates": [],
    }

    status = "existing"

    if previous_run_utc:
        first_seen = parse_dt(
            row["first_seen_utc"]
        )
        previous = parse_dt(
            previous_run_utc
        )
        content_changed = parse_dt(
            row["content_changed_utc"]
        )
        metrics_changed = parse_dt(
            row["metrics_changed_utc"]
        )

        if (
            first_seen
            and previous
            and first_seen > previous
        ):
            status = "new_since_previous_digest"

        elif (
            content_changed
            and previous
            and content_changed > previous
        ):
            status = "edited_since_previous_digest"

        elif (
            metrics_changed
            and previous
            and metrics_changed > previous
        ):
            status = "metrics_changed_since_previous_digest"

    else:
        status = "first_digest"

    message["change_status"] = status
    return message




def register_run(
    conn,
    hours,
    messages_exported,
    successful_channels,
    failed_channels,
    channels,
):
    now_utc = utc_now()
    now_local = datetime.now().astimezone()

    conn.execute(
        """
        INSERT INTO runs (
            created_utc,
            created_local,
            hours,
            messages_exported,
            successful_channels,
            failed_channels,
            latest_file,
            selection_fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            iso_utc(now_utc),
            now_local.isoformat(
                timespec="seconds"
            ),
            float(hours),
            int(messages_exported),
            int(successful_channels),
            int(failed_channels),
            str(LATEST_FILE),
            selection_fingerprint(channels),
        ),
    )
    conn.commit()


# ============================================================
# Telegram synchronization
# ============================================================

def new_api_safety_telemetry(channel_count):
    return {
        "sync_id": uuid.uuid4().hex[:12],
        "channel_count": int(channel_count),
        "channels_completed": 0,
        "channels_failed": 0,
        "messages_scanned": 0,
        "history_iterators_started": 0,
        "network_retry_count": 0,
        "flood_wait": False,
        "flood_wait_seconds": 0,
        "safety_halt": False,
        "halt_reason": None,
    }


def note_history_iterator_started(telemetry):
    if telemetry is not None:
        telemetry["history_iterators_started"] = (
            int(telemetry.get("history_iterators_started", 0)) + 1
        )


def note_network_retry(telemetry):
    if telemetry is not None:
        telemetry["network_retry_count"] = (
            int(telemetry.get("network_retry_count", 0)) + 1
        )


def note_flood_wait(telemetry, seconds):
    if telemetry is not None:
        telemetry["flood_wait"] = True
        telemetry["flood_wait_seconds"] = max(
            int(telemetry.get("flood_wait_seconds", 0)),
            max(0, int(seconds or 0)),
        )


def note_safety_halt(telemetry, reason):
    if telemetry is not None:
        telemetry["safety_halt"] = True
        telemetry["halt_reason"] = reason


def api_safety_summary_line(overall):
    api = overall.get("api_safety", {})
    halt_reason = api.get("halt_reason") or "none"
    return (
        "API_SAFETY_SUMMARY | "
        f"sync_id={api.get('sync_id') or 'unknown'} | "
        f"channel_count={int(api.get('channel_count', 0))} | "
        f"channels_completed={int(api.get('channels_completed', 0))} | "
        f"channels_failed={int(api.get('channels_failed', 0))} | "
        f"messages_scanned={int(api.get('messages_scanned', 0))} | "
        f"history_iterators_started={int(api.get('history_iterators_started', 0))} | "
        f"network_retries={int(api.get('network_retry_count', 0))} | "
        f"flood_wait={bool(api.get('flood_wait', False))} | "
        f"flood_wait_seconds={int(api.get('flood_wait_seconds', 0))} | "
        f"safety_halt={bool(api.get('safety_halt', False))} | "
        f"halt_reason={halt_reason} | "
        f"elapsed_seconds={float(api.get('elapsed_seconds', 0.0)):.3f}"
    )


def channel_has_recent_known_message(
    conn,
    channel_id,
    max_message_id,
    cutoff_dt,
):
    row = conn.execute(
        """
        SELECT 1
        FROM messages
        WHERE channel_id = ?
          AND message_id <= ?
          AND date_utc >= ?
        LIMIT 1
        """,
        (
            int(channel_id),
            int(max_message_id),
            iso_utc(cutoff_dt),
        ),
    ).fetchone()
    return row is not None


def init_sync_stats():
    return {
        "successful_channels": 0,
        "failed_channels": 0,
        "new_messages_saved": 0,
        "content_changed_messages_refreshed": 0,
        "metrics_changed_messages_refreshed": 0,
        "migrated_messages": 0,
        "unchanged_messages_refreshed": 0,
        "telegram_messages_scanned": 0,
        "channel_results": [],
    }


def init_channel_sync_stats(channel, first_sync):
    return {
        "channel": channel["name"],
        "username": channel.get(
            "username"
        ),
        "first_sync": first_sync,
        "new": 0,
        "content_changed": 0,
        "metrics_changed": 0,
        "migrated": 0,
        "same": 0,
        "scanned": 0,
        "status": "ok",
        "error": None,
    }


def apply_upsert_status(stats, status):
    if status == "new":
        stats["new"] += 1
    elif status == "content_changed":
        stats["content_changed"] += 1
    elif status == "metrics_changed":
        stats["metrics_changed"] += 1
    elif status == "content_and_metrics_changed":
        stats["content_changed"] += 1
        stats["metrics_changed"] += 1
    elif status == "migrated":
        stats["migrated"] += 1
    else:
        stats["same"] += 1


async def sync_channel_once(
    client,
    conn,
    channel,
    hours,
    settings,
    safety_telemetry=None,
):
    state = get_channel_state(
        conn,
        channel["id"],
    )

    last_message_id = (
        int(state["last_message_id"])
        if state
        and state.get("last_message_id")
        is not None
        else 0
    )

    max_seen_id = last_message_id
    first_sync = last_message_id <= 0

    stats = init_channel_sync_stats(
        channel,
        first_sync,
    )

    cutoff = utc_now() - timedelta(
        hours=hours
    )

    savepoint = (
        "channel_" +
        str(abs(int(channel["id"])))
    )

    conn.execute(
        f"SAVEPOINT {savepoint}"
    )

    try:
        # Первый запуск конкретного канала:
        # заполняем базу только за выбранный период.
        first_sync_complete = False

        if first_sync:
            note_history_iterator_started(safety_telemetry)
            async for msg in client.iter_messages(
                channel["entity"],
                wait_time=history_request_wait_seconds(settings),
            ):
                if (
                    getattr(msg, "date", None)
                    and msg.date < cutoff
                ):
                    first_sync_complete = True
                    break

                stats["scanned"] += 1
                max_seen_id = max(
                    max_seen_id,
                    int(msg.id),
                )

                item = make_message(
                    channel,
                    msg,
                    settings,
                )

                if item is None:
                    continue

                result = upsert_message(
                    conn,
                    item,
                )
                apply_upsert_status(
                    stats,
                    result,
                )
            else:
                # Итератор дошёл до начала истории канала.
                first_sync_complete = True

        # Все последующие запуски:
        # Telegram отдаёт только сообщения с ID выше последнего сохранённого.
        else:
            note_history_iterator_started(safety_telemetry)
            async for msg in client.iter_messages(
                channel["entity"],
                min_id=last_message_id,
                wait_time=history_request_wait_seconds(settings),
            ):
                stats["scanned"] += 1
                max_seen_id = max(
                    max_seen_id,
                    int(msg.id),
                )

                item = make_message(
                    channel,
                    msg,
                    settings,
                )

                if item is None:
                    continue

                result = upsert_message(
                    conn,
                    item,
                )
                apply_upsert_status(
                    stats,
                    result,
                )

        # Небольшое окно последних уже известных постов обновляем повторно
        # ради edit_date, просмотров, реакций и числа комментариев.
        #
        # На последующих запусках refresh можно безопасно пропустить,
        # если до начала sync в локальной базе не было известных сообщений
        # внутри refresh-окна: новые сообщения уже получены incremental-проходом.
        # Первый sync и каналы с недавними известными сообщениями сохраняют
        # отдельный refresh. Перед Stable это надёжнее сложной cursor-схемы и
        # сохраняет шанс поймать правки/метрики, изменившиеся во время backfill.
        refresh_limit = int(
            settings.get(
                "refresh_recent_messages",
                50,
            )
        )
        refresh_hours = float(
            settings.get(
                "refresh_recent_hours",
                2,
            )
        )
        refresh_cutoff = utc_now() - timedelta(
            hours=refresh_hours
        )
        refresh_needed = (
            refresh_limit > 0
            and (
                first_sync
                or refresh_hours <= 0
                or channel_has_recent_known_message(
                    conn,
                    channel["id"],
                    last_message_id,
                    refresh_cutoff,
                )
            )
        )

        if refresh_needed:
            note_history_iterator_started(safety_telemetry)
            async for msg in client.iter_messages(
                channel["entity"],
                limit=refresh_limit,
                wait_time=history_request_wait_seconds(settings),
            ):
                if (
                    refresh_hours > 0
                    and getattr(msg, "date", None)
                    and msg.date < refresh_cutoff
                ):
                    break

                # Не двигаем incremental cursor этим проходом. Если публикация
                # появилась уже после первого прохода, она останется выше
                # max_seen_id и будет гарантированно получена следующим sync.
                if int(msg.id) > max_seen_id:
                    continue

                item = make_message(
                    channel,
                    msg,
                    settings,
                )

                if item is None:
                    continue

                result = upsert_message(
                    conn,
                    item,
                )
                apply_upsert_status(
                    stats,
                    result,
                )

        stats["deletion_check"] = {
            "checked": 0,
            "reason": "disabled_for_speed",
        }
        upsert_channel_state(
            conn,
            channel,
            last_message_id=max_seen_id,
            success=True,
            error=None,
        )

        if first_sync_complete:
            cutoff_utc = iso_utc(cutoff)
            conn.execute(
                """
                UPDATE channels
                SET
                    history_coverage_utc = CASE
                        WHEN history_coverage_utc IS NULL
                          OR history_coverage_utc > ?
                        THEN ?
                        ELSE history_coverage_utc
                    END,
                    history_backfill_updated_utc = ?
                WHERE channel_id = ?
                """,
                (
                    cutoff_utc,
                    cutoff_utc,
                    iso_utc(utc_now()),
                    int(channel["id"]),
                ),
            )

        conn.execute(
            f"RELEASE SAVEPOINT {savepoint}"
        )
        conn.commit()

        return stats

    except Exception:
        conn.execute(
            f"ROLLBACK TO SAVEPOINT "
            f"{savepoint}"
        )
        conn.execute(
            f"RELEASE SAVEPOINT "
            f"{savepoint}"
        )
        conn.rollback()
        raise


async def sync_channel_with_retries(
    client,
    conn,
    channel,
    hours,
    settings,
    safety_telemetry=None,
):
    attempts = max(
        1,
        int(
            settings.get(
                "channel_retry_attempts",
                3,
            )
        ),
    )

    base_delay = max(
        1,
        int(
            settings.get(
                "retry_delay_seconds",
                3,
            )
        ),
    )

    last_error = None
    halt_sync = False
    halt_reason = None

    for attempt in range(
        1,
        attempts + 1,
    ):
        try:
            if not client.is_connected():
                await client.connect()

            result = await sync_channel_once(
                client,
                conn,
                channel,
                hours,
                settings,
                safety_telemetry=safety_telemetry,
            )
            return result

        except FloodWaitError as e:
            last_error = (
                f"FloodWait {e.seconds} сек."
            )
            halt_sync = True
            halt_reason = "flood_wait"
            note_flood_wait(safety_telemetry, e.seconds)
            note_safety_halt(safety_telemetry, halt_reason)

            log_error(
                f"{channel['name']}: {last_error}; "
                "сетевой этап остановлен без автоматического повтора."
            )
            print(
                f"    Telegram вернул FloodWait {e.seconds} сек. "
                "Автоповтор отключён; текущая синхронизация "
                "останавливается."
            )
            break

        except Exception as e:
            last_error = (
                f"{type(e).__name__}: {e}"
            )

            if telegram_error_requires_safety_stop(e):
                halt_sync = True
                halt_reason = "account_or_api_restriction"
                note_safety_halt(safety_telemetry, halt_reason)
                log_error(
                    f"{channel['name']} | SAFETY STOP | "
                    f"{last_error}"
                )
                print(
                    "    Telegram вернул ошибку авторизации/ограничения. "
                    "Автоповтор отключён; текущая синхронизация "
                    "останавливается."
                )
                break

            log_error(
                f"{channel['name']} | "
                f"попытка {attempt}/{attempts} | "
                f"{last_error}"
            )

            if attempt < attempts:
                note_network_retry(safety_telemetry)
                delay = base_delay * attempt
                print(
                    f"    Временная ошибка; "
                    f"повтор через {delay} сек..."
                )
                await asyncio.sleep(delay)

    upsert_channel_state(
        conn,
        channel,
        success=False,
        error=last_error,
    )
    conn.commit()

    return {
        "channel": channel["name"],
        "username": channel.get(
            "username"
        ),
        "first_sync": False,
        "new": 0,
        "content_changed": 0,
        "metrics_changed": 0,
        "migrated": 0,
        "same": 0,
        "scanned": 0,
        "status": "error",
        "error": last_error,
        "halt_sync": halt_sync,
        "halt_reason": halt_reason,
    }


async def sync_all_channels(
    client,
    conn,
    channels,
    hours,
    settings,
):
    total_channels = len(channels)
    if total_channels > MAX_SELECTED_CHANNELS:
        message = channel_limit_message(total_channels)
        log_error("SYNC_BLOCKED | " + message)
        raise ValueError(message)

    overall = init_sync_stats()
    started_at = time.monotonic()
    channel_delay = inter_channel_delay_seconds(
        settings,
        total_channels,
    )
    history_wait = history_request_wait_seconds(settings)

    overall["api_safety"] = new_api_safety_telemetry(
        total_channels
    )
    overall["api_safety"].update({
        "history_request_wait_seconds": history_wait,
        "inter_channel_delay_seconds": channel_delay,
        # Совместимость с уже существующими потребителями внутренних stats.
        "halted_by_flood_wait": False,
        "halted_by_api_safety": False,
    })

    log_info(
        "SYNC_START | "
        f"channels={total_channels} | hours={hours} | "
        f"history_wait={history_wait:.2f}s | "
        f"channel_delay={channel_delay:.2f}s"
    )

    print(
        "\nСинхронизирую Telegram с локальной базой..."
    )

    for index, channel in enumerate(
        channels,
        1,
    ):
        print(
            f"  [{index}/{len(channels)}] "
            f"{channel['name']}"
        )

        result = await sync_channel_with_retries(
            client,
            conn,
            channel,
            hours,
            settings,
            safety_telemetry=overall["api_safety"],
        )

        overall["channel_results"].append(
            result
        )

        overall["telegram_messages_scanned"] += (
            result.get("scanned", 0)
        )

        if result["status"] == "ok":
            overall[
                "successful_channels"
            ] += 1

            overall[
                "new_messages_saved"
            ] += result.get("new", 0)

            overall[
                "content_changed_messages_refreshed"
            ] += result.get(
                "content_changed",
                0,
            )

            overall[
                "metrics_changed_messages_refreshed"
            ] += result.get(
                "metrics_changed",
                0,
            )

            overall[
                "migrated_messages"
            ] += result.get(
                "migrated",
                0,
            )

            overall[
                "unchanged_messages_refreshed"
            ] += result.get(
                "same",
                0,
            )

            mode = (
                "первичное заполнение"
                if result.get("first_sync")
                else "догружено новое"
            )

            print(
                f"      OK — {mode}; "
                f"новых {result.get('new', 0)}, "
                f"содержание изменено {result.get('content_changed', 0)}, "
                f"метрики обновлены {result.get('metrics_changed', 0)}"
            )

        else:
            overall[
                "failed_channels"
            ] += 1

            print(
                f"      ОШИБКА — "
                f"{result.get('error')}"
            )

        if result.get("halt_sync"):
            reason = result.get("halt_reason") or "api_safety"
            overall["api_safety"][
                "halted_by_api_safety"
            ] = True
            overall["api_safety"]["halt_reason"] = reason
            note_safety_halt(overall["api_safety"], reason)
            if reason == "flood_wait":
                overall["api_safety"][
                    "halted_by_flood_wait"
                ] = True
            print(
                "      ЗАЩИТНАЯ ОСТАНОВКА: после сигнала Telegram "
                "остальные каналы в этом запуске не запрашиваются. "
                f"Причина: {reason}."
            )
            break

        if index < total_channels and channel_delay > 0:
            await asyncio.sleep(channel_delay)

    overall["duration_seconds"] = round(
        time.monotonic() - started_at,
        3,
    )
    overall["api_safety"].update({
        "channels_completed": overall["successful_channels"],
        "channels_failed": overall["failed_channels"],
        "messages_scanned": overall["telegram_messages_scanned"],
        "elapsed_seconds": overall["duration_seconds"],
    })
    overall["api_safety_summary"] = api_safety_summary_line(
        overall
    )
    log_info(overall["api_safety_summary"])
    log_info(
        "SYNC_END | "
        f"channels_done={len(overall['channel_results'])}/"
        f"{total_channels} | "
        f"failed={overall['failed_channels']} | "
        f"scanned={overall['telegram_messages_scanned']} | "
        f"halted={overall['api_safety']['halted_by_api_safety']} | "
        f"halt_reason={overall['api_safety']['halt_reason']} | "
        f"duration={overall['duration_seconds']:.3f}s"
    )

    return overall



# ============================================================
# Historical backfill for objective topic search
# ============================================================

def get_channel_history_coverage(conn, channel_id):
    row = conn.execute(
        """
        SELECT
            history_coverage_utc,
            history_backfill_updated_utc
        FROM channels
        WHERE channel_id = ?
        """,
        (int(channel_id),),
    ).fetchone()

    if not row:
        return None

    return row["history_coverage_utc"]


def set_channel_history_coverage(conn, channel_id, cutoff_utc):
    now = iso_utc(utc_now())

    conn.execute(
        """
        UPDATE channels
        SET
            history_coverage_utc = ?,
            history_backfill_updated_utc = ?
        WHERE channel_id = ?
        """,
        (
            cutoff_utc,
            now,
            int(channel_id),
        ),
    )
    conn.commit()


def channel_db_bounds(conn, channel_id):
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS c,
            MIN(message_id) AS min_id,
            MAX(message_id) AS max_id,
            MIN(date_utc) AS min_date,
            MAX(date_utc) AS max_date
        FROM messages
        WHERE channel_id = ?
        """,
        (int(channel_id),),
    ).fetchone()

    return {
        "messages": int(row["c"] or 0),
        "min_id": (
            int(row["min_id"])
            if row["min_id"] is not None
            else None
        ),
        "max_id": (
            int(row["max_id"])
            if row["max_id"] is not None
            else None
        ),
        "earliest_utc": row["min_date"],
        "latest_utc": row["max_date"],
    }


async def backfill_channel_history_once(
    client,
    conn,
    channel,
    cutoff_dt,
    settings,
):
    """
    Догружает старые посты конкретного канала назад до cutoff_dt.

    Если история уже была подтверждённо заполнена до нужной даты,
    повторно Telegram не читаем.
    """
    cutoff_utc = iso_utc(cutoff_dt)
    known_coverage = get_channel_history_coverage(
        conn,
        channel["id"],
    )

    known_dt = parse_dt(known_coverage)

    if known_dt and known_dt <= cutoff_dt:
        bounds = channel_db_bounds(
            conn,
            channel["id"],
        )
        return {
            "channel": channel["name"],
            "username": channel.get("username"),
            "status": "already_complete",
            "complete": True,
            "downloaded": 0,
            "scanned": 0,
            "coverage_utc": known_coverage,
            "earliest_utc": bounds["earliest_utc"],
            "latest_utc": bounds["latest_utc"],
            "error": None,
        }

    bounds = channel_db_bounds(
        conn,
        channel["id"],
    )

    # Если база уже содержит сообщения канала, начинаем строго старше
    # самого раннего сохранённого message_id. Иначе идём от текущего конца.
    kwargs = {}
    if known_dt is not None and bounds["min_id"] is not None:
        kwargs["max_id"] = int(bounds["min_id"])

    downloaded = 0
    scanned = 0
    reached_cutoff = False
    history_exhausted = False

    async for msg in client.iter_messages(
        channel["entity"],
        wait_time=history_request_wait_seconds(settings),
        **kwargs,
    ):
        scanned += 1

        msg_date = getattr(
            msg,
            "date",
            None,
        )

        if (
            msg_date
            and msg_date < cutoff_dt
        ):
            reached_cutoff = True
            break

        item = make_message(
            channel,
            msg,
            settings,
        )

        if item is None:
            continue

        result = upsert_message(
            conn,
            item,
        )

        if result in (
            "new",
            "content_changed",
            "content_and_metrics_changed",
        ):
            downloaded += 1

    else:
        # iterator завершился сам: мы дошли до самого начала истории канала.
        history_exhausted = True

    complete = (
        reached_cutoff
        or history_exhausted
    )

    if complete:
        set_channel_history_coverage(
            conn,
            channel["id"],
            cutoff_utc,
        )

    conn.commit()

    bounds_after = channel_db_bounds(
        conn,
        channel["id"],
    )

    return {
        "channel": channel["name"],
        "username": channel.get("username"),
        "status": (
            "backfilled"
            if complete
            else "partial"
        ),
        "complete": complete,
        "downloaded": downloaded,
        "scanned": scanned,
        "coverage_utc": (
            cutoff_utc
            if complete
            else known_coverage
        ),
        "earliest_utc": bounds_after["earliest_utc"],
        "latest_utc": bounds_after["latest_utc"],
        "error": None,
    }


async def backfill_channel_history_with_retries(
    client,
    conn,
    channel,
    cutoff_dt,
    settings,
):
    attempts = max(
        1,
        int(
            settings.get(
                "channel_retry_attempts",
                3,
            )
        ),
    )

    base_delay = max(
        1,
        int(
            settings.get(
                "retry_delay_seconds",
                3,
            )
        ),
    )

    last_error = None
    halt_sync = False
    halt_reason = None

    for attempt in range(
        1,
        attempts + 1,
    ):
        try:
            return await backfill_channel_history_once(
                client,
                conn,
                channel,
                cutoff_dt,
                settings,
            )

        except FloodWaitError as e:
            last_error = f"FloodWait {e.seconds} сек."
            halt_sync = True
            halt_reason = "flood_wait"

            log_error(
                f"BACKFILL {channel['name']}: {last_error}; "
                "догрузка остановлена без автоматического повтора."
            )
            print(
                f"      Telegram вернул FloodWait {e.seconds} сек. "
                "Догрузка истории остановлена."
            )
            break

        except Exception as e:
            last_error = (
                f"{type(e).__name__}: {e}"
            )

            if telegram_error_requires_safety_stop(e):
                halt_sync = True
                halt_reason = "account_or_api_restriction"
                log_error(
                    f"BACKFILL {channel['name']} | SAFETY STOP | "
                    f"{last_error}"
                )
                print(
                    "      Telegram вернул ошибку авторизации/ограничения. "
                    "Догрузка истории остановлена."
                )
                break

            log_error(
                f"BACKFILL {channel['name']} | "
                f"попытка {attempt}/{attempts} | "
                f"{last_error}"
            )

            if attempt < attempts:
                delay = base_delay * attempt
                print(
                    f"      Ошибка истории; "
                    f"повтор через {delay} сек..."
                )
                await asyncio.sleep(delay)

    return {
        "channel": channel["name"],
        "username": channel.get("username"),
        "status": "error",
        "complete": False,
        "downloaded": 0,
        "scanned": 0,
        "coverage_utc": get_channel_history_coverage(
            conn,
            channel["id"],
        ),
        "earliest_utc": None,
        "latest_utc": None,
        "error": last_error,
        "halt_sync": halt_sync,
        "halt_reason": halt_reason,
    }


async def _v4_ensure_history_for_search(
    client,
    conn,
    channels,
    days,
    settings,
):
    """
    Перед поиском:
    1) догружает всё новое из Telegram;
    2) проверяет покрытие каждого выбранного канала;
    3) при необходимости догружает старую историю до requested cutoff.

    После первого заполнения за 90 дней будущие поиски обычно сводятся
    только к быстрой догрузке новых сообщений.
    """
    retention_days = max(
        1,
        int(
            settings.get(
                "database_retention_days",
                90,
            )
        ),
    )
    effective_days = min(
        max(1, int(days)),
        retention_days,
    )

    cutoff_dt = utc_now() - timedelta(
        days=effective_days
    )

    print(
        "\nОбновляю базу перед поиском..."
    )

    # Сначала обязательно получаем всё, что появилось после последнего
    # обычного дайджеста. Для нового канала первый sync сразу берёт весь
    # requested period.
    sync_stats = await sync_all_channels(
        client,
        conn,
        channels,
        effective_days * 24,
        settings,
    )

    api_safety = sync_stats.get("api_safety", {})
    safety_halted = api_safety.get(
        "halted_by_api_safety",
        api_safety.get("halted_by_flood_wait", False),
    )
    if safety_halted:
        print(
            "\nДогрузка старой истории пропущена: "
            "предыдущий сетевой этап остановлен защитой API."
        )
        return {
            "requested_days": int(days),
            "effective_days": effective_days,
            "cutoff_utc": iso_utc(cutoff_dt),
            "selected_channels": len(channels),
            "complete_channels": 0,
            "incomplete_channels": len(channels),
            "complete": False,
            "sync_quality": {
                "successful_channels": sync_stats.get(
                    "successful_channels",
                    0,
                ),
                "failed_channels": sync_stats.get(
                    "failed_channels",
                    0,
                ),
                "new_messages_saved": sync_stats.get(
                    "new_messages_saved",
                    0,
                ),
            },
            "channel_coverage": [],
            "warnings": [
                {
                    "channel": None,
                    "error": (
                        "Догрузка истории отменена после "
                        "защитной остановки Telegram API: "
                        f"{api_safety.get('halt_reason') or 'ограничение'}."
                    ),
                    "coverage_utc": None,
                }
            ],
        }

    print(
        f"\nПроверяю полноту истории за "
        f"{effective_days} дней..."
    )

    results = []
    total_channels = len(channels)
    channel_delay = inter_channel_delay_seconds(
        settings,
        total_channels,
    )

    for index, channel in enumerate(
        channels,
        1,
    ):
        print(
            f"  [{index}/{len(channels)}] "
            f"{channel['name']}"
        )

        result = await backfill_channel_history_with_retries(
            client,
            conn,
            channel,
            cutoff_dt,
            settings,
        )
        results.append(result)

        if result["complete"]:
            if result["downloaded"]:
                print(
                    f"      история дополнена: "
                    f"+{result['downloaded']} сообщений"
                )
            else:
                print(
                    "      история уже полная"
                )
        else:
            print(
                f"      НЕПОЛНО: "
                f"{result.get('error') or 'не удалось подтвердить покрытие'}"
            )

        if result.get("halt_sync"):
            print(
                "      ЗАЩИТНАЯ ОСТАНОВКА: остальные каналы "
                "истории в этом запуске не запрашиваются."
            )
            break

        if index < total_channels and channel_delay > 0:
            await asyncio.sleep(channel_delay)

    complete_channels = sum(
        1 for x in results
        if x.get("complete")
    )

    failed = [
        {
            "channel": x["channel"],
            "error": x.get("error"),
            "coverage_utc": x.get("coverage_utc"),
        }
        for x in results
        if not x.get("complete")
    ]

    return {
        "requested_days": int(days),
        "effective_days": effective_days,
        "cutoff_utc": iso_utc(cutoff_dt),
        "selected_channels": len(channels),
        "complete_channels": complete_channels,
        "incomplete_channels": (
            len(channels) - complete_channels
        ),
        "complete": (
            complete_channels == len(channels)
            and sync_stats.get(
                "failed_channels",
                0,
            ) == 0
        ),
        "sync_quality": {
            "successful_channels": sync_stats.get(
                "successful_channels",
                0,
            ),
            "failed_channels": sync_stats.get(
                "failed_channels",
                0,
            ),
            "new_messages_saved": sync_stats.get(
                "new_messages_saved",
                0,
            ),
        },
        "channel_coverage": results,
        "warnings": failed,
    }


async def prefill_full_history(
    client,
    conn,
    channels,
    settings,
):
    days = max(
        1,
        int(
            settings.get(
                "database_retention_days",
                90,
            )
        ),
    )

    print(
        f"\nЗаполняю локальную историю "
        f"за максимум {days} дней."
    )
    print(
        "Первый запуск может занять заметное время; "
        "процесс безопасно продолжится при следующем запуске."
    )

    status = await ensure_history_for_search(
        client,
        conn,
        channels,
        days,
        settings,
    )

    if status["complete"]:
        print(
            f"\nГОТОВО: история всех "
            f"{status['selected_channels']} каналов "
            f"полностью покрывает последние {days} дней."
        )
    else:
        print(
            f"\nВНИМАНИЕ: полное покрытие получено для "
            f"{status['complete_channels']}/"
            f"{status['selected_channels']} каналов."
        )

    return status


# ============================================================
# Deduplication / operational split
# ============================================================

def build_related_groups(messages):
    """
    Связывает сообщения только по сильным origin/source признакам.

    Telegram forward origin остаётся сильным признаком сам по себе.
    Внешняя URL используется консервативно: она должна выглядеть как
    конкретный материал, встречаться в разных каналах и не повторяться
    внутри одного канала. Это не event clustering и не дедупликация.
    """
    parent = {}
    key_to_message = {}

    for message in messages:
        key = message_key(message)
        parent[key] = key
        key_to_message[key] = message
        message.pop("related_group_id", None)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    forward_buckets = {}
    url_buckets = {}

    def add(bucket, relation, key):
        bucket.setdefault(relation, [])
        if key not in bucket[relation]:
            bucket[relation].append(key)

    for message in messages:
        key = message_key(message)
        origin_key = message.get("origin_key")

        if isinstance(origin_key, str):
            if origin_key.startswith("telegram_forward:"):
                add(forward_buckets, origin_key, key)
            elif origin_key.startswith("url:"):
                url = origin_key[4:]
                if is_specific_shared_source_url(url):
                    add(url_buckets, url, key)

        for url in message.get("canonical_urls", []) or []:
            if is_specific_shared_source_url(url):
                add(url_buckets, url, key)

    # Точная пересылка одного Telegram-поста — сильный origin.
    for keys in forward_buckets.values():
        if len(keys) < 2:
            continue
        first = keys[0]
        for key in keys[1:]:
            union(first, key)

    # Внешняя URL — более слабый сигнал. Одинаковая ссылка внутри одного
    # канала часто оказывается постоянным promo/service элементом, поэтому
    # не должна связывать публикации без дополнительного сильного признака.
    for keys in url_buckets.values():
        if len(keys) < 2:
            continue

        channel_keys = []
        repeated_within_channel = False
        seen_channels = set()

        for key in keys:
            message = key_to_message[key]
            channel_identity = (
                message.get("channel_id"),
                message.get("username"),
                message.get("channel"),
            )
            if channel_identity in seen_channels:
                repeated_within_channel = True
                break
            seen_channels.add(channel_identity)
            channel_keys.append(key)

        if repeated_within_channel or len(seen_channels) < 2:
            continue

        first = channel_keys[0]
        for key in channel_keys[1:]:
            union(first, key)

    groups = {}

    for key in parent:
        root = find(key)
        groups.setdefault(root, []).append(key)

    output = []
    group_index = 1

    for keys in groups.values():
        if len(keys) < 2:
            continue

        members = [key_to_message[key] for key in keys]

        channels = unique_keep_order(
            m.get("channel")
            for m in members
        )

        canonical_urls = unique_keep_order(
            url
            for m in members
            for url in m.get("canonical_urls", [])
        )

        origin_keys = unique_keep_order(
            m.get("origin_key")
            for m in members
            if m.get("origin_key")
        )

        group_id = f"related_{group_index:04d}"
        group_index += 1

        for message in members:
            message["related_group_id"] = group_id

        output.append({
            "group_id": group_id,
            "message_refs": [
                {
                    "message_key": message_key(m),
                    "channel": m.get("channel"),
                    "username": m.get("username"),
                    "message_id": m.get("message_id"),
                    "channel_url": m.get("channel_url")
                    or public_channel_link(m.get("username")),
                    "telegram_url": m.get("telegram_url"),
                }
                for m in members
            ],
            "channels": channels,
            "canonical_urls": canonical_urls,
            "origin_keys": origin_keys,
            "note": (
                "Подсказка о возможном общем конкретном источнике. "
                "Она не доказывает, что сообщения описывают одно событие."
            ),
        })

    return output



def _continuity_message_tokens(message):
    """
    Небольшой набор содержательных лексем для консервативного
    сопоставления текущего сообщения с недавней предысторией.

    URL удаляются заранее: постоянная профильная/рекламная ссылка не должна
    становиться лексическим признаком одного сюжета.
    """
    continuity_stopwords = {
        # Минимальный отдельный слой для слов, которые могут случайно выглядеть
        # "редкими" в коротком трёхдневном окне, но сами по себе не описывают
        # событие. Основной набор служебных слов переиспользуется из поиска.
        "часть", "части", "частью", "частина", "частини",
        "прямо", "всего", "всього", "просто", "более", "больше", "більше",
        "около", "близько", "также", "також",
    }
    text = re.sub(
        r"https?://\S+",
        " ",
        str(message.get("text") or ""),
        flags=re.IGNORECASE,
    )
    tokens = re.findall(
        r"[^\W_]+(?:['’-][^\W_]+)*",
        unicodedata.normalize("NFKC", text).casefold(),
        flags=re.UNICODE,
    )
    result = set()
    for token in tokens:
        if len(token) < 5 or token.isdigit():
            continue
        normalized = normalize_search_token(token)
        if (
            normalized in SEARCH_STOPWORDS
            or normalized in continuity_stopwords
        ):
            continue
        # search_stem_prefix определён ниже в модуле; к моменту выполнения
        # дайджеста модуль уже полностью загружен.
        stem = search_stem_prefix(normalized)
        if (
            len(stem) >= 5
            and stem not in SEARCH_STOPWORDS
            and stem not in continuity_stopwords
        ):
            result.add(stem)
    return result



def _continuity_event_anchor_pairs(message):
    """
    Редкие локальные пары в начале сообщения для отделения продолжения
    конкретного эпизода от просто похожей темы.

    Используется только как дополнительный gate для lexical_candidate.
    Strong-сигналы (reply и общий конкретный внешний источник) от этого
    фильтра не зависят.
    """
    allowed = _continuity_message_tokens(message)
    if not allowed:
        return set()

    text = re.sub(
        r"https?://\S+",
        " ",
        str(message.get("text") or ""),
        flags=re.IGNORECASE,
    )
    first_paragraph = re.split(
        r"\n\s*\n",
        text,
        maxsplit=1,
    )[0]
    raw_tokens = re.findall(
        r"[^\W_]+(?:['’-][^\W_]+)*",
        unicodedata.normalize("NFKC", first_paragraph).casefold(),
        flags=re.UNICODE,
    )

    lead_tokens = []
    for token in raw_tokens:
        normalized = normalize_search_token(token)
        stem = search_stem_prefix(normalized)
        if stem not in allowed:
            continue
        lead_tokens.append(stem)
        if len(lead_tokens) >= CONTINUITY_EVENT_ANCHOR_LEAD_TOKENS:
            break

    pairs = set()
    for left_index, left in enumerate(lead_tokens):
        upper = min(
            len(lead_tokens),
            left_index + 1 + CONTINUITY_EVENT_ANCHOR_PAIR_WINDOW,
        )
        for right_index in range(left_index + 1, upper):
            right = lead_tokens[right_index]
            if left != right:
                pairs.add(tuple(sorted((left, right))))
    return pairs


def _continuity_attribution_anchor_tokens(message):
    """
    Return lexical-anchor noise from an explicit multiword source attribution.

    A construction such as "пишет Financial Times со ссылкой ..." may be
    useful provenance, but the publisher name and the surrounding attribution
    boilerplate are not an event identity.  Keep normal message tokens intact;
    this helper is used only by the pure lexical event-anchor gate.
    """
    text = re.sub(
        r"https?://\S+",
        " ",
        str(message.get("text") or ""),
        flags=re.IGNORECASE,
    )
    first_paragraph = re.split(
        r"\n\s*\n",
        text,
        maxsplit=1,
    )[0]

    result = set()
    for match in re.finditer(
        r"\b(?:сообща\w*|пиш\w*|переда\w*)\s+"
        r"(?P<source>[^\n,.;:()]{1,80}?)\s+"
        r"(?:со|с|з)\s+ссылк\w*",
        first_paragraph,
        flags=re.IGNORECASE | re.UNICODE,
    ):
        source_tokens = []
        for token in re.findall(
            r"[^\W_]+(?:['’-][^\W_]+)*",
            unicodedata.normalize(
                "NFKC",
                match.group("source"),
            ).casefold(),
            flags=re.UNICODE,
        ):
            if len(token) < 5 or token.isdigit():
                continue
            normalized = normalize_search_token(token)
            if normalized in SEARCH_STOPWORDS:
                continue
            token_stem = search_stem_prefix(normalized)
            if len(token_stem) >= 5:
                source_tokens.append(token_stem)

        # A single source word (for example Reuters) is too broad a reason to
        # alter lexical grouping.  The RUN 3 failures came from multiword
        # publisher/source names acting as rare anchor pairs.
        if len(set(source_tokens)) < 2:
            continue

        for token in re.findall(
            r"[^\W_]+(?:['’-][^\W_]+)*",
            unicodedata.normalize(
                "NFKC",
                match.group(0),
            ).casefold(),
            flags=re.UNICODE,
        ):
            if len(token) < 5 or token.isdigit():
                continue
            normalized = normalize_search_token(token)
            if normalized in SEARCH_STOPWORDS:
                continue
            token_stem = search_stem_prefix(normalized)
            if len(token_stem) >= 5:
                result.add(token_stem)

    return result


def _continuity_recurring_summary_day(message):
    """
    Возвращает календарный день только для явно периодической сводки в lead.

    Это не общий стоп-лист: признак применяется лишь внутри pure lexical
    event-anchor gate, чтобы соседние суточные отчёты одного канала не
    превращались автоматически в один эпизод.
    """
    text = str(message.get("text") or "")
    first_paragraph = re.split(
        r"\n\s*\n",
        text,
        maxsplit=1,
    )[0]
    folded = unicodedata.normalize("NFKC", first_paragraph).casefold()
    if not re.search(
        r"(?:\bза\s+(?:добу|сутки)\b|\bсуточн\w*\s+сводк\w*\b)",
        folded,
        flags=re.UNICODE,
    ):
        return None

    value = message.get("date_local") or message.get("date_utc")
    parsed = parse_dt(value)
    if not parsed:
        return None
    return parsed.date()


def _continuity_specific_origin(message):
    origin = message.get("origin_key")
    if not isinstance(origin, str) or not origin:
        return None
    if origin.startswith("url:") and not is_specific_shared_source_url(
        origin[4:]
    ):
        return None
    return origin


def _continuity_specific_urls(message):
    return {
        url
        for url in message.get("canonical_urls", []) or []
        if is_specific_shared_source_url(url)
    }


def _continuity_same_channel(first, second):
    first_id = int(first.get("channel_id") or 0)
    second_id = int(second.get("channel_id") or 0)
    if first_id and second_id:
        return first_id == second_id

    first_username = str(first.get("username") or "").lstrip("@").casefold()
    second_username = str(second.get("username") or "").lstrip("@").casefold()
    return bool(first_username and first_username == second_username)


def _continuity_url_owner_name(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return None

    host = (parts.hostname or "").lower()
    segments = [
        segment
        for segment in (parts.path or "").strip("/").split("/")
        if segment
    ]

    if host in {"t.me", "telegram.me"}:
        if len(segments) < 2 or not segments[-1].isdigit():
            return None
        if segments[0].lower() == "s" and len(segments) >= 3:
            return segments[1].lstrip("@").casefold()
        if segments[0].lower() == "c":
            return None
        return segments[0].lstrip("@").casefold()

    if host == "max.ru" and len(segments) >= 2:
        if segments[0].lower() in {"join", "joinchannel"}:
            return None
        return segments[0].lstrip("@").casefold()

    return None


def _continuity_url_owner_channel_id(url):
    """
    Для приватных Telegram-ссылок t.me/c/<channel_id>/<message_id>
    username недоступен, но внутренний id канала присутствует в URL.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return None

    host = (parts.hostname or "").lower()
    segments = [
        segment
        for segment in (parts.path or "").strip("/").split("/")
        if segment
    ]
    if (
        host in {"t.me", "telegram.me"}
        and len(segments) >= 3
        and segments[0].lower() == "c"
        and segments[1].isdigit()
        and segments[-1].isdigit()
    ):
        return int(segments[1])
    return None


def _continuity_message_public_names(message):
    names = set()
    username = str(message.get("username") or "").lstrip("@").casefold()
    if username:
        names.add(username)

    channel_url = message.get("channel_url")
    if isinstance(channel_url, str):
        try:
            parts = urlsplit(channel_url.strip())
        except Exception:
            parts = None
        if parts:
            host = (parts.hostname or "").lower()
            segments = [
                segment
                for segment in (parts.path or "").strip("/").split("/")
                if segment
            ]
            if host in {"t.me", "telegram.me", "max.ru"} and segments:
                candidate = segments[0].lstrip("@").casefold()
                if candidate and candidate not in {"join", "joinchannel", "c", "s"}:
                    names.add(candidate)
    return names


def _continuity_is_same_channel_self_link(current_message, previous_message, url):
    """
    Конкретная ссылка на старый пост того же канала — полезный дополнительный
    сигнал, но не самостоятельное доказательство продолжения сюжета.
    """
    if not _continuity_same_channel(current_message, previous_message):
        return False

    owner_channel_id = _continuity_url_owner_channel_id(url)
    if owner_channel_id is not None:
        message_channel_id = abs(
            int(current_message.get("channel_id") or 0)
        )
        comparable_channel_ids = {message_channel_id}
        # В некоторых представлениях peer id канала содержит префикс -100,
        # тогда как t.me/c хранит только собственно channel_id.
        channel_id_text = str(message_channel_id)
        if channel_id_text.startswith("100") and len(channel_id_text) > 3:
            comparable_channel_ids.add(int(channel_id_text[3:]))
        return bool(
            message_channel_id
            and owner_channel_id in comparable_channel_ids
        )

    owner = _continuity_url_owner_name(url)
    if not owner:
        return False

    return owner in (
        _continuity_message_public_names(current_message)
        | _continuity_message_public_names(previous_message)
    )


def load_recent_continuity_messages(
    channels,
    period_start_utc,
    reference_utc=None,
    lookback_days=CONTINUITY_LOOKBACK_DAYS,
    source_digest_limit=CONTINUITY_SOURCE_DIGESTS,
):
    """
    Загружает только сообщения ДО текущего периода из нескольких последних
    JSON-дайджестов с тем же точным набором каналов.

    Архив используется как bounded cache: это быстрее и безопаснее, чем
    повторно обходить всю SQLite-историю или делать дополнительные Telegram
    запросы. Перекрывающиеся архивы дедуплицируются по message_key.
    """
    fingerprint = selection_fingerprint(channels)

    def normalize_dt(value):
        if isinstance(value, datetime):
            dt = value
        else:
            dt = parse_dt(value)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    period_start = normalize_dt(period_start_utc)
    upper = normalize_dt(reference_utc) or utc_now()
    upper = upper.astimezone(timezone.utc)
    cutoff = upper - timedelta(days=max(1, int(lookback_days)))
    if period_start is None:
        period_start = upper

    candidates = []
    if LATEST_FILE.exists():
        candidates.append(LATEST_FILE)
    if ARCHIVE_DIR.exists():
        try:
            candidates.extend(
                sorted(
                    ARCHIVE_DIR.glob("*.json"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
        except Exception:
            pass

    seen_paths = set()
    seen_digest_times = set()
    seen_messages = set()
    result = []

    for path in candidates:
        try:
            path_key = str(Path(path).resolve())
        except Exception:
            path_key = str(path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        try:
            payload = json.loads(
                Path(path).read_text(encoding="utf-8")
            )
        except Exception:
            continue

        meta = payload.get("meta") or {}
        if meta.get("artifact_type") not in (None, "telegram_news_digest"):
            continue
        if meta.get("selection_fingerprint") != fingerprint:
            continue

        created = normalize_dt(
            meta.get("created_utc") or meta.get("created_local")
        )
        if not created or created < cutoff or created > upper + timedelta(minutes=5):
            continue

        created_key = iso_utc(created)
        if created_key in seen_digest_times:
            continue
        seen_digest_times.add(created_key)

        for message in payload.get("news_messages") or []:
            if not isinstance(message, dict):
                continue
            message_dt = normalize_dt(
                message.get("date_utc") or message.get("date_local")
            )
            if (
                not message_dt
                or message_dt >= period_start
                or message_dt < cutoff
            ):
                continue
            key = (
                message.get("message_key")
                or message_key(message)
            )
            if key in seen_messages:
                continue
            seen_messages.add(key)
            result.append(copy.deepcopy(message))

        if len(seen_digest_times) >= max(1, int(source_digest_limit)):
            break

    result.sort(
        key=lambda message: (
            message.get("date_utc")
            or message.get("date_local")
            or ""
        ),
        reverse=True,
    )
    return result


def build_continuity_context(
    current_messages,
    prior_messages,
    *,
    lookback_days=CONTINUITY_LOOKBACK_DAYS,
    max_messages=CONTINUITY_MAX_MESSAGES,
    per_current=CONTINUITY_PER_CURRENT_MESSAGE,
):
    """
    Строит ограниченный контекст предыстории.

    Безусловно сильный сигнал — reply на старое сообщение. Общий конкретный
    внешний источник тоже остаётся strong, но внутренняя ссылка на старый пост
    того же канала становится только дополнительным признаком и требует
    содержательного lexical_candidate. Чистая лексическая связь использует
    только допустимые токены, текущие пороги и редкий локальный event-anchor
    в начале обоих сообщений.
    """
    current = [
        message
        for message in current_messages
        if isinstance(message, dict)
        and (message.get("text") or "").strip()
    ]
    prior = [
        message
        for message in prior_messages
        if isinstance(message, dict)
        and (message.get("text") or "").strip()
    ]

    empty = {
        "lookback_days": int(lookback_days),
        "messages_count": 0,
        "messages": [],
        "note": (
            "Предыстория вне выбранного периода. Используй её только для "
            "понимания текущего развития; lexical_candidate — лишь подсказка, "
            "а не доказательство одного события."
        ),
    }
    if not current or not prior:
        return empty

    prior_by_key = {}
    prior_tokens = {}
    prior_token_docs = {}
    prior_anchor_pairs = {}
    origin_index = {}
    url_index = {}
    direct_index = {}

    for message in prior:
        key = message.get("message_key") or message_key(message)
        prior_by_key[key] = message
        direct_index[(
            int(message.get("channel_id") or 0),
            int(message.get("message_id") or 0),
        )] = key

        origin = _continuity_specific_origin(message)
        if origin:
            origin_index.setdefault(origin, set()).add(key)

        for url in _continuity_specific_urls(message):
            url_index.setdefault(url, set()).add(key)

        tokens = _continuity_message_tokens(message)
        prior_tokens[key] = tokens
        for token in tokens:
            prior_token_docs.setdefault(token, set()).add(key)

        prior_anchor_pairs[key] = _continuity_event_anchor_pairs(message)

    current_tokens_by_key = {}
    current_anchor_pairs_by_key = {}
    token_document_frequency = Counter()
    anchor_pair_document_frequency = Counter()
    for tokens in prior_tokens.values():
        token_document_frequency.update(tokens)
    for pairs in prior_anchor_pairs.values():
        anchor_pair_document_frequency.update(pairs)

    for message in current:
        key = message.get("message_key") or message_key(message)
        tokens = _continuity_message_tokens(message)
        current_tokens_by_key[key] = tokens
        token_document_frequency.update(tokens)

        anchor_pairs = _continuity_event_anchor_pairs(message)
        current_anchor_pairs_by_key[key] = anchor_pairs
        anchor_pair_document_frequency.update(anchor_pairs)

    document_count = max(
        1,
        len(prior_tokens) + len(current_tokens_by_key),
    )
    rare_limit = max(3, int(math.ceil(document_count * 0.03)))
    common_limit = max(8, int(math.ceil(document_count * 0.12)))
    anchor_pair_limit = max(2, int(math.ceil(document_count * 0.01)))
    anchor_token_limit = max(3, int(math.ceil(document_count * 0.015)))

    linked = {}

    def add_link(prior_key, current_message, reasons, strength, score):
        record = linked.setdefault(
            prior_key,
            {
                "message": prior_by_key[prior_key],
                "links": {},
                "best_strength": 0,
                "best_score": 0.0,
            },
        )
        current_key = (
            current_message.get("message_key")
            or message_key(current_message)
        )
        link = record["links"].setdefault(
            current_key,
            {
                "message_ref": make_compact_message_ref(current_message),
                "match_reasons": set(),
                "match_strength": strength,
                "score": float(score),
                "current_date": (
                    current_message.get("date_utc")
                    or current_message.get("date_local")
                    or ""
                ),
            },
        )
        link["match_reasons"].update(reasons)
        if strength > link["match_strength"]:
            link["match_strength"] = strength
        link["score"] = max(link["score"], float(score))
        record["best_strength"] = max(record["best_strength"], strength)
        record["best_score"] = max(record["best_score"], float(score))

    for current_message in current:
        current_key = (
            current_message.get("message_key")
            or message_key(current_message)
        )
        candidates = {}
        soft_source_reasons = {}

        def mark(prior_key, reason, strength=2, score=100.0):
            if not prior_key or prior_key == current_key:
                return
            if prior_key not in prior_by_key:
                return
            item = candidates.setdefault(
                prior_key,
                {
                    "reasons": set(),
                    "strength": 0,
                    "score": 0.0,
                },
            )
            item["reasons"].add(reason)
            item["strength"] = max(item["strength"], strength)
            item["score"] = max(item["score"], float(score))

        def mark_soft_source(prior_key, reason):
            if not prior_key or prior_key == current_key:
                return
            if prior_key not in prior_by_key:
                return
            soft_source_reasons.setdefault(prior_key, set()).add(reason)

        reply_to = current_message.get("reply_to_message_id")
        if reply_to is not None:
            mark(
                direct_index.get((
                    int(current_message.get("channel_id") or 0),
                    int(reply_to),
                )),
                "reply_to_previous_message",
            )

        origin = _continuity_specific_origin(current_message)
        if origin:
            for prior_key in origin_index.get(origin, set()):
                previous = prior_by_key[prior_key]
                if (
                    origin.startswith("url:")
                    and _continuity_is_same_channel_self_link(
                        current_message,
                        previous,
                        origin[4:],
                    )
                ):
                    mark_soft_source(prior_key, "same_specific_origin")
                else:
                    mark(prior_key, "same_specific_origin")

        for url in _continuity_specific_urls(current_message):
            for prior_key in url_index.get(url, set()):
                previous = prior_by_key[prior_key]
                if _continuity_is_same_channel_self_link(
                    current_message,
                    previous,
                    url,
                ):
                    mark_soft_source(prior_key, "same_specific_url")
                else:
                    mark(prior_key, "same_specific_url")

        current_tokens = current_tokens_by_key.get(current_key, set())
        eligible_current = {
            token
            for token in current_tokens
            if token_document_frequency.get(token, 0) <= common_limit
        }
        candidate_counts = Counter()
        for token in eligible_current:
            for prior_key in prior_token_docs.get(token, ()):
                candidate_counts[prior_key] += 1

        for prior_key, shared_count in candidate_counts.items():
            if prior_key == current_key or shared_count < 3:
                continue

            previous = prior_by_key[prior_key]
            previous_set = prior_tokens.get(prior_key, set())
            eligible_previous = {
                token
                for token in previous_set
                if token_document_frequency.get(token, 0) <= common_limit
            }
            eligible_shared = eligible_current & eligible_previous
            if len(eligible_shared) < 3:
                continue

            rare_shared = sum(
                1
                for token in eligible_shared
                if token_document_frequency.get(token, 0) <= rare_limit
            )

            same_channel = _continuity_same_channel(
                current_message,
                previous,
            )
            if same_channel:
                if len(eligible_shared) < 3 or rare_shared < 2:
                    continue
            else:
                if len(eligible_shared) < 4 or rare_shared < 3:
                    continue

            overlap = len(eligible_shared) / max(
                1,
                min(len(eligible_current), len(eligible_previous)),
            )
            if overlap < 0.20:
                continue

            idf_score = sum(
                math.log(
                    (document_count + 1)
                    / (token_document_frequency.get(token, 0) + 1)
                ) + 1.0
                for token in eligible_shared
            )
            lexical_score_value = (
                idf_score
                + len(eligible_shared) * 0.5
                + overlap * 4.0
            )
            if lexical_score_value < 5.0:
                continue

            current_summary_day = _continuity_recurring_summary_day(
                current_message
            )
            previous_summary_day = _continuity_recurring_summary_day(
                previous
            )
            if (
                same_channel
                and current_summary_day is not None
                and previous_summary_day is not None
                and current_summary_day != previous_summary_day
            ):
                continue

            shared_anchor_pairs = (
                current_anchor_pairs_by_key.get(current_key, set())
                & prior_anchor_pairs.get(prior_key, set())
            )
            has_event_anchor = any(
                anchor_pair_document_frequency.get(pair, 0)
                <= anchor_pair_limit
                and min(
                    token_document_frequency.get(pair[0], document_count),
                    token_document_frequency.get(pair[1], document_count),
                )
                <= anchor_token_limit
                for pair in shared_anchor_pairs
            )
            if not has_event_anchor:
                continue

            mark(
                prior_key,
                "lexical_candidate",
                strength=1,
                score=lexical_score_value,
            )
            for reason in soft_source_reasons.get(prior_key, ()):
                mark(
                    prior_key,
                    reason,
                    strength=1,
                    score=lexical_score_value,
                )

        ordered = sorted(
            candidates.items(),
            key=lambda item: (
                item[1]["strength"],
                item[1]["score"],
                prior_by_key[item[0]].get("date_utc")
                or prior_by_key[item[0]].get("date_local")
                or "",
            ),
            reverse=True,
        )
        for prior_key, info in ordered[:max(1, int(per_current))]:
            add_link(
                prior_key,
                current_message,
                info["reasons"],
                info["strength"],
                info["score"],
            )

    ordered_context = sorted(
        linked.items(),
        key=lambda item: (
            item[1]["best_strength"],
            item[1]["best_score"],
            item[1]["message"].get("date_utc")
            or item[1]["message"].get("date_local")
            or "",
        ),
        reverse=True,
    )[:max(0, int(max_messages))]

    exported = []
    for _, record in ordered_context:
        context_message = prepare_message_for_ai(
            copy.deepcopy(record["message"])
        )
        # Старый change_status/related_group_id относится к прежнему выпуску
        # и не должен выглядеть как статус текущего периода.
        context_message.pop("change_status", None)
        context_message.pop("related_group_id", None)

        ordered_links = sorted(
            record["links"].values(),
            key=lambda item: (
                item["match_strength"],
                item["score"],
                item["current_date"],
            ),
            reverse=True,
        )[:CONTINUITY_RELATED_CURRENT_LIMIT]

        links = []
        for item in ordered_links:
            links.append({
                "message_ref": item["message_ref"],
                "match_reasons": sorted(item["match_reasons"]),
                "match_strength": (
                    "strong"
                    if item["match_strength"] >= 2
                    else "candidate"
                ),
            })

        exported.append({
            "context_message": context_message,
            "related_current_message_refs": links,
        })

    result = dict(empty)
    result["messages_count"] = len(exported)
    result["messages"] = exported
    return result


def split_operational(
    messages,
    settings,
):
    if not settings.get(
        "separate_routine_alerts",
        True,
    ):
        return messages, []

    news = []
    operational = []

    for message in messages:
        if is_routine_alert(
            message["text"]
        ):
            operational.append(message)
        else:
            news.append(message)

    return news, operational


# ============================================================
# Output / cleanup
# ============================================================

EDITORIAL_PRINCIPLES = (
    "Сначала учти весь набор текущих сообщений. Не заканчивай дайджест после нескольких самых заметных историй. Каждый "
    "самостоятельный содержательно значимый сюжет раскрой либо помести в «Коротко»: полнота важной повестки важнее искусственной "
    "краткости. Одиночное важное сообщение нельзя терять из-за отсутствия повторов. Всегда игнорируй сведения о пользователе, "
    "персональную память и историю текущего и прошлых чатов: они не источник фактов и не должны влиять на отбор, порядок, акценты "
    "или оценку полезности материала. Не пиши «для вас», «вам особенно важно». Тематика заранее неизвестна: темы выбирай по "
    "фактическому материалу, без фиксированных рубрик и географических приоритетов. Объединяй только один конкретный эпизод или его "
    "непосредственное развитие: общая тема, страна или организация сами по себе не означают одно событие. Для развивающегося сюжета "
    "сначала установи самое позднее состояние по времени: позднее уточнение имеет приоритет, но не стирай развитие; при изменении "
    "пиши «Первоначально сообщалось…, позже выяснилось…». Перед итогом проверь более поздние сообщения: если позднее сообщение "
    "опровергает, блокирует, уточняет, отменяет или меняет статус ранней версии, отрази это. Не повышай уверенность относительно источника: "
    "«возможно», «по данным источника» и "
    "«предположительно» нельзя превращать в факт. Различай прямое сообщение, независимое подтверждение, официальное заявление, "
    "пересказ первоисточника, инсайд, версию и редакционный вывод. Названный первоисточник не превращает утверждение в факт. "
    "Количество публикаций само по себе не делает сюжет важнее и не повышает достоверность. Перепечатки одного исходного сообщения "
    "не считай независимыми подтверждениями. Уверенность при необходимости: «подтверждено», «вероятно», «пока не подтверждено», "
    "«спорно», «мнение/оценка». Расхождения показывай по совпадающим и спорным деталям. similar_message_refs и близость формулировок — "
    "подсказка; это не доказательство одного события или подтверждения. Не используй общие знания модели для «улучшения» новости. "
    "Не добавляй существенные факты, географию, участников, мотивы, последствия или причинность, которых нет в переданном материале. "
    "Если данных для вывода недостаточно, скажи, что это неясно. Контекст и вывод добавляй только из материала; если факты "
    "самодостаточны, не дописывай обязательную аналитику и не ранжируй событие. Внешние источники используй точечно для первоисточника, "
    "документа, цифры или необходимого контекста. Служебные поля используй только для сопоставления; в готовом ответе процесс не показывай. "
    "Не обсуждай файл, JSON, локальную базу, синхронизацию, дедупликацию или алгоритм поиска. Пиши плотно и профессионально; сохраняй "
    "имена, даты, числа и степень уверенности. Пиши на языке запроса пользователя; если язык запроса не указан или неясен, пиши по-русски. "
    "Иноязычные публикации переводи по смыслу, сохраняя имена, числа, цитируемые факты и ссылки. Не разделяй источники по языку: "
    "русско-, украино- и англоязычные сообщения одного события объединяй в один сюжет. Заголовки делай короткими и не сильнее данных. "
)


SOURCE_RULES = (
    "Каждый самостоятельный фактический сюжет завершай строкой источника, если есть SOURCE_URL, telegram_url или channel_url. После "
    "строки источника не добавляй фактов. Для составного сюжета источники должны покрывать существенные утверждения; иначе раздели "
    "сюжет. В «Коротко» ставь источник после каждого события; «Главное за период» может не дублировать ссылки. При SOURCE_URL/telegram_url "
    "используй ровно эту строку и копируй ДОСЛОВНО, иначе используй channel_url. Печатай URL обычным текстом, не Markdown-ссылкой: "
    "**Источник:** Канал — https://t.me/... . URL не придумывай: не заменяй t.me Google или redirect-ссылкой, не добавляй utm_source, "
    "не сокращай, не нормализуй, не меняй query и не «исправляй» по памяти. При сомнении выведи исходную literal URL. Для простого "
    "сюжета достаточно одного содержательного источника; для составного — 2–3 ключевых. Одинаковые перепечатки не перечисляй. "
)


DIGEST_REQUEST = (
    "Подготовь редакторский дайджест за выбранный период. У точных повторов inherited_fields восстанавливай по "
    "inherits_from_message_key. Основной дайджест всегда строй по всему содержательному материалу news_messages и operational_messages. "
    "continuity_context — предыстория; lexical_candidate не доказательство, context_message не выдавай за текущую новость. "
    "changes_since_previous_digest — только дополнительный слой сравнения: он не задаёт временные границы основного дайджеста и не "
    "является фильтром отбора. outside_period_changes используй только для развития сюжета или «Что изменилось» и не расширяй ими "
    "основной временной интервал. Если changes_since_previous_digest.comparison_available=true, учти новые и изменённые публикации. "
    "Сообщения предыдущего выпуска не исключай из основного дайджеста, если они нужны для полной картины периода. Изменение только "
    "метрик новой новостью не считай. related_message_groups — только подсказка о возможном общем источнике, а не приказ объединять "
    "сообщения в одно событие. "
    + EDITORIAL_PRINCIPLES +
    SOURCE_RULES +
    "В заголовке укажи дату и фактический локальный интервал всего охваченного материала по date_local, а не только сообщений из "
    "блока сравнения; если надёжно определить интервал нельзя, не придумывай. При насыщенном материале сразу переходи к «Главное за "
    "период»; не ставь перед ним второй абзац с тем же резюме. Если важных событий мало, блок не нужен; глубину каждого сюжета определяй "
    "количеством реально новой информации; не ограничивай этим число сюжетов. Содержательно значимые события без отдельного разбора "
    "собери в «Коротко» вместо того, чтобы опустить их. Однотипные оперативные предупреждения одного сюжета объединяй. После «Главное "
    "за период» не добавляй повторный итог, личный выбор или рейтинг. «Что изменилось» не заменяет основной дайджест и добавляется только "
    "при существенных изменениях. Не объясняй читателю внутренние правила охвата и сравнения: молча применяй полный период основного "
    "выпуска и дополнительную роль блока изменений, не комментируя их в готовом тексте. Отсутствие новых сообщений не считай событием. "
)


def calculate_change_summary(messages):
    result = {
        "new_since_previous_digest": 0,
        "edited_since_previous_digest": 0,
        "metrics_changed_since_previous_digest": 0,
        "unavailable_since_previous_digest": 0,
        "existing_in_period": 0,
        "first_digest": 0,
    }

    for message in messages:
        status = message.get(
            "change_status",
            "existing",
        )

        if status in result:
            result[status] += 1
        else:
            result[
                "existing_in_period"
            ] += 1

    result["existing"] = result.pop(
        "existing_in_period"
    )
    return result


def make_compact_message_ref(message):
    return {
        "message_key": message.get(
            "message_key"
        ) or message_key(message),
        "channel_id": message.get(
            "channel_id"
        ),
        "message_id": message.get(
            "message_id"
        ),
        "channel": message.get(
            "channel"
        ),
        "username": message.get(
            "username"
        ),
        "channel_url": message.get(
            "channel_url"
        ) or public_channel_link(message.get("username")),
        "telegram_url": message.get(
            "telegram_url"
        ),
        "date_local": message.get(
            "date_local"
        ),
    }


def prepare_message_for_ai(message):
    """
    Убирает из AI-ориентированного JSON только восстановимые повторы,
    пустые коллекции и служебные значения по умолчанию.
    """
    result = copy.deepcopy(message)

    def compact(item):
        if not isinstance(item, dict):
            return

        raw_text = item.get("raw_text")
        text = item.get("text")
        if (
            item.get("raw_text_available") is True
            and isinstance(raw_text, str)
            and isinstance(text, str)
            and raw_text.strip() == text
        ):
            item.pop("raw_text", None)
            item.pop("raw_text_available", None)

        if item.get("in_selected_period") is True:
            item.pop("in_selected_period", None)

        if item.get("availability") == "available":
            item.pop("availability", None)
        item.pop("availability_checked_utc", None)
        if item.get("unavailable_since_utc") is None:
            item.pop("unavailable_since_utc", None)

        for key in (
            "reactions",
            "external_urls",
            "canonical_urls",
            "duplicates",
            "previous_versions",
        ):
            if item.get(key) == []:
                item.pop(key, None)

        media = item.get("media")
        if isinstance(media, dict) and not any(
            value is not None
            for value in media.values()
        ):
            item.pop("media", None)

        if item.get("versions_count") == 0:
            item.pop("versions_count", None)
            item.pop("versions_truncated", None)
            item.pop("previous_versions", None)

        for key in (
            "username",
            "channel_url",
            "telegram_url",
            "edit_date_utc",
            "edit_date_local",
            "views",
            "forwards",
            "replies",
            "album_id",
            "reply_to_message_id",
            "post_author",
            "forwarded_from",
        ):
            if item.get(key) is None:
                item.pop(key, None)

        for key in ("duplicates", "previous_versions"):
            for child in item.get(key) or []:
                compact(child)

    compact(result)
    return result



def _ai_material_external_urls(message):
    """
    Сохраняет исходные URL конкретных материалов, но не постоянные
    profile/promo/service ссылки. URL не нормализуются в пользовательском файле.
    """
    result = []
    source_url = message.get("telegram_url")
    channel_url = message.get("channel_url")

    for raw_url in message.get("external_urls", []) or []:
        if not isinstance(raw_url, str):
            continue
        raw_url = raw_url.strip()
        if not raw_url or raw_url in {source_url, channel_url}:
            continue
        normalized = normalize_external_url(raw_url)
        if normalized and is_specific_shared_source_url(normalized):
            result.append(raw_url)

    return unique_keep_order(result)


def _ai_message_block(message, label="MESSAGE", related_current_refs=None):
    key = message.get("message_key") or message_key(message)
    text = str(message.get("text") or "").strip()
    lines = [
        "---",
        f"[{label} {key}]",
        f"DATE_LOCAL: {message.get('date_local') or message.get('date_utc') or ''}",
        f"CHANNEL: {message.get('channel') or ''}",
    ]

    username = message.get("username")
    if username:
        lines.append(f"USERNAME: @{username}")

    source_url = (
        message.get("telegram_url")
        or message.get("channel_url")
        or public_channel_link(message.get("username"))
    )
    if source_url:
        lines.append(f"SOURCE_URL: {source_url}")

    related_group_id = message.get("related_group_id")
    if related_group_id:
        lines.append(f"RELATED_HINT: {related_group_id}")

    reply_to = message.get("reply_to_message_id")
    if reply_to is not None:
        lines.append(f"REPLY_TO_MESSAGE_ID: {reply_to}")

    forwarded = message.get("forwarded_from")
    if isinstance(forwarded, dict):
        forward_bits = []
        if forwarded.get("chat_title"):
            forward_bits.append(str(forwarded["chat_title"]))
        if forwarded.get("chat_username"):
            forward_bits.append("@" + str(forwarded["chat_username"]))
        if forwarded.get("channel_post") is not None:
            forward_bits.append("post=" + str(forwarded["channel_post"]))
        if forwarded.get("date_utc"):
            forward_bits.append("date=" + str(forwarded["date_utc"]))
        if forward_bits:
            lines.append("FORWARD_CONTEXT: " + " | ".join(forward_bits))
        if forwarded.get("telegram_url"):
            lines.append("FORWARD_SOURCE_URL: " + str(forwarded["telegram_url"]))

    media = message.get("media")
    if isinstance(media, dict) and media.get("type"):
        lines.append("MEDIA: " + str(media["type"]))
    if text.startswith("[Медиа без подписи:"):
        lines.append("MEDIA_ONLY: true")

    external_urls = _ai_material_external_urls(message)
    if external_urls:
        lines.append("EXTERNAL_URLS:")
        lines.extend("- " + url for url in external_urls)

    if related_current_refs:
        refs = []
        for item in related_current_refs:
            if isinstance(item, dict):
                ref = item.get("message_ref") or item.get("message_key")
            else:
                ref = item
            if ref:
                refs.append(str(ref))
        if refs:
            lines.append("RELATED_CURRENT_REFS: " + ", ".join(refs))

    lines.append("TEXT:")
    lines.append(text or "[Нет текстового содержимого]")
    return "\n".join(lines)


def _is_textless_media_only_for_ai(message):
    """True для медиа без доступного модели содержательного текста."""
    media = message.get("media")
    if not (isinstance(media, dict) and media.get("type")):
        return False
    text = str(message.get("text") or "").strip()
    return (
        not text
        or (
            text.startswith("[Медиа без подписи:")
            and text.endswith("]")
        )
    )



CANDIDATE_GUIDANCE = (
    "Каждый CANDIDATE — кандидат на самостоятельный сюжет, а не утверждение, что все его сообщения точно описывают одно событие. "
    "Разрешается объединять несколько CANDIDATE, если EVIDENCE показывает один эпизод, но содержательно значимый singleton нельзя молча терять. "
    "MEMBER_REFS и SUPPORTING_REFS обеспечивают покрытие; факты и степень уверенности бери из EVIDENCE."
)


def _event_candidate_sort_key(message):
    return (
        message.get("date_local")
        or message.get("date_utc")
        or "",
        int(message.get("channel_id") or 0),
        int(message.get("message_id") or 0),
    )


def _event_candidate_channel_identity(message):
    channel_id = int(message.get("channel_id") or 0)
    if channel_id:
        return ("id", channel_id)
    username = str(message.get("username") or "").lstrip("@").casefold()
    if username:
        return ("username", username)
    return ("channel", str(message.get("channel") or "").casefold())


def _event_candidate_ref(message, retained_as=None):
    result = {
        "message_key": message.get("message_key") or message_key(message),
        "channel": message.get("channel"),
        "date_local": message.get("date_local") or message.get("date_utc"),
        "source_url": (
            message.get("telegram_url")
            or message.get("channel_url")
            or public_channel_link(message.get("username"))
        ),
    }
    if retained_as:
        result["retained_as"] = retained_as
    return result


def _event_candidate_named_token(fragment):
    """Return one high-confidence person-like proper token from a short fragment."""
    tokens = re.findall(
        r"[A-ZА-ЯЁІЇЄҐ][^\W\d_]*(?:[’'\-][A-ZА-ЯЁІЇЄҐa-zа-яёіїєґ][^\W\d_]*)*",
        str(fragment or ""),
        flags=re.UNICODE,
    )
    tokens = [
        token
        for token in tokens
        if len(token) >= 4 and not token.isupper()
    ]
    if not tokens:
        return None
    normalized = normalize_search_token(tokens[-1])
    return search_stem_prefix(normalized) or None


def _event_candidate_meeting_participants(message):
    """
    Extract an ordered (main participant, counterpart) pair only from explicit
    meeting/talk wording. This is deliberately small and deterministic: it is
    used solely to veto a proven false-positive current-message reply edge.
    """
    text = unicodedata.normalize(
        "NFKC",
        str(message.get("text") or ""),
    )[:320]
    if not text:
        return None

    # RU/UA: "A встретился/зустрівся с/з B".
    match = re.search(
        r"(?P<before>.{0,100}?)\b(?:встрет\w*|зустр\w*)\b\s+"
        r"(?:с|со|з|зі|із)\s+(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor = _event_candidate_named_token(match.group("before"))
        counterpart_fragment = re.split(
            r"\s+(?:в|у|на|під|под|біля|около|at|in|on|during)\s+",
            match.group("after"),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        counterpart = _event_candidate_named_token(counterpart_fragment)
        if actor and counterpart and actor != counterpart:
            return (actor, counterpart)

    # EN: "A will meet/met/meets [with] B".
    match = re.search(
        r"(?P<before>.{0,100}?)\b(?:will\s+meet|met|meets?)\b\s+"
        r"(?:with\s+)?(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor = _event_candidate_named_token(match.group("before"))
        counterpart_fragment = re.split(
            r"\s+(?:at|in|on|during|after|before|where|who|which)\s+",
            match.group("after"),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        counterpart = _event_candidate_named_token(counterpart_fragment)
        if actor and counterpart and actor != counterpart:
            return (actor, counterpart)

    # RU/UA/EN joint subject: "A и/та/and B проведут встречу / will meet".
    match = re.search(
        r"(?P<subjects>[^\n.!?]{1,140}?)\s+\b(?:"
        r"провед\w*\s+(?:встреч\w*|зустріч\w*|переговор\w*|перемов\w*)"
        r"|(?:will\s+)?meet|hold\w*\s+(?:a\s+)?meeting)\b",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        parts = re.split(
            r"\s+(?:и|та|and|&)\s+",
            match.group("subjects"),
            flags=re.IGNORECASE,
        )
        if len(parts) >= 2:
            actor = _event_candidate_named_token(parts[-2])
            counterpart = _event_candidate_named_token(parts[-1])
            if actor and counterpart and actor != counterpart:
                return (actor, counterpart)

    # RU/UA: "A проведёт встречу/переговоры с B".
    match = re.search(
        r"(?P<before>.{0,100}?)\bпровед\w*\s+"
        r"(?:встреч\w*|зустріч\w*|переговор\w*|перемов\w*)\s+"
        r"(?:с|со|з|зі|із)\s+(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor = _event_candidate_named_token(match.group("before"))
        counterpart = _event_candidate_named_token(match.group("after"))
        if actor and counterpart and actor != counterpart:
            return (actor, counterpart)

    return None


def _event_candidate_reply_counterparty_diverges(message, target_message):
    left = _event_candidate_meeting_participants(message)
    right = _event_candidate_meeting_participants(target_message)
    if not left or not right:
        return False
    return left[0] == right[0] and left[1] != right[1]


def _event_candidate_lexical_actor_token(fragment):
    fragment = re.split(
        r"[:;]\s*",
        str(fragment or ""),
    )[-1]
    return _event_candidate_named_token(fragment)


def _event_candidate_lexical_counterpart_token(fragment):
    fragment = re.split(
        r"[,;:]|\s+(?:в|у|на|під|под|біля|около|at|in|on|during|"
        r"after|before|where|who|which)\s+",
        str(fragment or ""),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _event_candidate_named_token(fragment)


def _event_candidate_lexical_meeting_pair(message):
    """
    Return one unordered high-confidence meeting pair for pure lexical joining.

    This parser is intentionally independent from the accepted reply guard.
    It supports only explicit person-pair forms and avoids treating a generic
    "зустрічі з делегацією" phrase or trailing source attribution as a person.
    """
    text = unicodedata.normalize(
        "NFKC",
        str(message.get("text") or ""),
    )[:320]
    if not text:
        return None

    # RU/UA explicit verb: "A встретился / зустрівся з B".
    # Ukrainian noun "зустрічі" is deliberately excluded here.
    match = re.search(
        r"(?P<before>.{0,100}?)\b(?:встрет\w*|"
        r"зустр(?:ів|іла|іли|ін|іча)\w*)\b\s+"
        r"(?:с|со|з|зі|із)\s+(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor = _event_candidate_lexical_actor_token(
            match.group("before")
        )
        counterpart = _event_candidate_lexical_counterpart_token(
            match.group("after")
        )
        if actor and counterpart and actor != counterpart:
            return tuple(sorted((actor, counterpart)))

    # EN: "A will meet/met/meets [with] B".
    match = re.search(
        r"(?P<before>.{0,100}?)\b(?:will\s+meet|met|meets?)\b\s+"
        r"(?:with\s+)?(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor = _event_candidate_lexical_actor_token(
            match.group("before")
        )
        counterpart = _event_candidate_lexical_counterpart_token(
            match.group("after")
        )
        if actor and counterpart and actor != counterpart:
            return tuple(sorted((actor, counterpart)))

    # Joint subject: "A и/та/and B проведут встречу / will meet".
    match = re.search(
        r"(?P<subjects>[^\n.!?]{1,140}?)\s+\b(?:"
        r"провед\w*\s+(?:встреч\w*|зустріч\w*|"
        r"переговор\w*|перемов\w*)"
        r"|(?:will\s+)?meet|hold\w*\s+(?:a\s+)?meeting)\b",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        subjects = re.split(
            r"[:;]\s*",
            match.group("subjects"),
        )[-1]
        parts = re.split(
            r"\s+(?:и|та|and|&)\s+",
            subjects,
            flags=re.IGNORECASE,
        )
        if len(parts) >= 2:
            actor = _event_candidate_named_token(parts[-2])
            counterpart = _event_candidate_named_token(parts[-1])
            if actor and counterpart and actor != counterpart:
                return tuple(sorted((actor, counterpart)))

    # RU/UA: "A проведёт встречу/переговоры с B".
    match = re.search(
        r"(?P<before>.{0,100}?)\bпровед\w*\s+"
        r"(?:встреч\w*|зустріч\w*|переговор\w*|перемов\w*)\s+"
        r"(?:с|со|з|зі|із)\s+(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor = _event_candidate_lexical_actor_token(
            match.group("before")
        )
        counterpart = _event_candidate_lexical_counterpart_token(
            match.group("after")
        )
        if actor and counterpart and actor != counterpart:
            return tuple(sorted((actor, counterpart)))

    # Noun-led headline: "Встреча A с B" / "Зустріч A з B".
    # Reject an immediate preposition after the noun; otherwise a phrase like
    # "із зустрічі з делегацією" can scan ahead and invent an actor.
    match = re.search(
        r"\b(?:встреч(?:а|и|у|ей)|зустріч(?:і|у)?)\s+"
        r"(?P<actor>(?!(?:с|со|з|зі|із)\b)[^\n.!?]{1,80}?)\s+"
        r"(?:с|со|з|зі|із)\s+(?P<after>[^\n.!?]{1,100})",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        actor_fragment = match.group("actor")
        actor_words = re.findall(
            r"[^\W_]+(?:['’-][^\W_]+)*",
            actor_fragment,
            flags=re.UNICODE,
        )
        if len(actor_words) <= 6:
            actor = _event_candidate_named_token(actor_fragment)
            counterpart = _event_candidate_lexical_counterpart_token(
                match.group("after")
            )
            if actor and counterpart and actor != counterpart:
                return tuple(sorted((actor, counterpart)))

    # Noun-led scheduled/status form: "Зустріч A і B буде ...".
    match = re.search(
        r"\b(?:встреч(?:а|и|у)|зустріч(?:і|у)?)\s+"
        r"(?P<subjects>[^\n.!?]{1,120}?)\s+"
        r"\b(?:буд\w*|відбуд\w*|состо\w*|заплан\w*|"
        r"ожида\w*|очіку\w*|возмож\w*|можлив\w*)\b",
        text,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if match:
        subjects = re.split(
            r"[:;]\s*",
            match.group("subjects"),
        )[-1]
        parts = re.split(
            r"\s+(?:и|та|and|&)\s+",
            subjects,
            flags=re.IGNORECASE,
        )
        if len(parts) >= 2:
            actor = _event_candidate_named_token(parts[-2])
            counterpart = _event_candidate_named_token(parts[-1])
            if actor and counterpart and actor != counterpart:
                return tuple(sorted((actor, counterpart)))

    return None


def _event_candidate_person_token_matches(left, right):
    # This comparison is intentionally permissive.  It is only used to avoid a
    # false veto when the same person appears in inflected RU/UA forms or close
    # transliterations; failing to match simply means "do not block".
    translation = str.maketrans({
        "ь": "",
        "ъ": "",
        "і": "и",
        "ї": "и",
        "є": "е",
        "э": "е",
        "ы": "и",
    })
    left = str(left or "").translate(translation)
    right = str(right or "").translate(translation)
    if left == right:
        return True
    return (
        min(len(left), len(right)) >= 6
        and (
            left.startswith(right)
            or right.startswith(left)
        )
    )


def _event_candidate_meeting_pairs_match(left, right):
    return (
        _event_candidate_person_token_matches(left[0], right[0])
        and _event_candidate_person_token_matches(left[1], right[1])
    ) or (
        _event_candidate_person_token_matches(left[0], right[1])
        and _event_candidate_person_token_matches(left[1], right[0])
    )


def _event_candidate_lexical_meeting_pair_diverges(message, target_message):
    left = _event_candidate_lexical_meeting_pair(message)
    right = _event_candidate_lexical_meeting_pair(target_message)
    return bool(
        left
        and right
        and not _event_candidate_meeting_pairs_match(left, right)
    )


def build_event_candidates(current_messages):
    """
    Deterministic CURRENT -> CURRENT coverage layer for the AI-facing export.

    Strong relations are closed transitively first. Pure lexical joining is then
    processed newest-first and may attach only to the fixed newest anchor of an
    already-created candidate. A peripheral lexical member therefore cannot
    bridge A-B and B-C into an automatic A-B-C merge.
    """
    messages = [
        message
        for message in current_messages
        if isinstance(message, dict)
    ]
    by_key = {}
    for message in messages:
        key = message.get("message_key") or message_key(message)
        if key in by_key:
            raise ValueError(
                "event candidate coverage invariant failed: duplicate input "
                f"message_key {key}"
            )
        by_key[key] = message

    if not messages:
        return {
            "candidates": [],
            "coverage": {
                "input_current_messages": 0,
                "event_candidates": 0,
                "singleton_candidates": 0,
                "full_evidence_messages": 0,
                "near_duplicate_supporting_refs": 0,
                "unassigned_messages": 0,
                "duplicate_assignments": 0,
            },
            "stats": {
                "strong_groups": 0,
                "lexical_assisted_candidates": 0,
                "max_candidate_size": 0,
                "average_candidate_size": 0.0,
                "median_candidate_size": 0.0,
            },
        }

    parent = {key: key for key in by_key}
    relation_edges = []

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left, right, relation_type, reason):
        if not left or not right or left == right:
            return
        if left not in parent or right not in parent:
            return
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root
        relation_edges.append((left, right, relation_type, reason))

    def union_bucket(keys, relation_type, reason):
        unique = list(dict.fromkeys(keys))
        if len(unique) < 2:
            return
        first = unique[0]
        for key in unique[1:]:
            union(first, key, relation_type, reason)

    related_buckets = {}
    forward_buckets = {}
    url_buckets = {}
    direct_index = {}

    for message in messages:
        key = message.get("message_key") or message_key(message)
        related_group_id = message.get("related_group_id")
        if related_group_id:
            related_buckets.setdefault(str(related_group_id), []).append(key)

        origin = _continuity_specific_origin(message)
        if isinstance(origin, str):
            if origin.startswith("telegram_forward:"):
                forward_buckets.setdefault(origin, []).append(key)
            elif origin.startswith("url:"):
                url = origin[4:]
                if is_specific_shared_source_url(url):
                    url_buckets.setdefault(url, []).append(key)

        for url in _continuity_specific_urls(message):
            url_buckets.setdefault(url, []).append(key)

        channel_id = int(message.get("channel_id") or 0)
        message_id = int(message.get("message_id") or 0)
        if channel_id and message_id:
            direct_index[(channel_id, message_id)] = key

    for keys in related_buckets.values():
        union_bucket(keys, "strong_source", "related_message_group")

    for keys in forward_buckets.values():
        union_bucket(keys, "strong_source", "telegram_forward_origin")

    # Keep the corrected source rule used by related_message_groups:
    # one concrete external material may join different channels, but a URL
    # repeated inside one channel is treated as potentially recurring/promo.
    for url, keys in url_buckets.items():
        unique = list(dict.fromkeys(keys))
        if len(unique) < 2:
            continue
        identities = []
        repeated_channel = False
        seen = set()
        for key in unique:
            identity = _event_candidate_channel_identity(by_key[key])
            if identity in seen:
                repeated_channel = True
                break
            seen.add(identity)
            identities.append(key)
        if repeated_channel or len(seen) < 2:
            continue
        union_bucket(
            identities,
            "strong_source",
            "same_specific_external_source",
        )

    for message in messages:
        key = message.get("message_key") or message_key(message)
        reply_to = message.get("reply_to_message_id")
        if reply_to is not None:
            target = direct_index.get((
                int(message.get("channel_id") or 0),
                int(reply_to),
            ))
            if not (
                target
                and _event_candidate_reply_counterparty_diverges(
                    message,
                    by_key[target],
                )
            ):
                union(
                    key,
                    target,
                    "strong_source",
                    "reply_to_current_message",
                )

        for ref in message.get("similar_message_refs", []) or []:
            if isinstance(ref, str) and ref in by_key:
                union(
                    key,
                    ref,
                    "near_duplicate",
                    "similar_message_ref",
                )

    component_members = {}
    for key in by_key:
        component_members.setdefault(find(key), []).append(key)

    component_relations = {
        root: {"types": set(), "reasons": set()}
        for root in component_members
    }
    for left, right, relation_type, reason in relation_edges:
        root = find(left)
        if root == find(right):
            component_relations[root]["types"].add(relation_type)
            component_relations[root]["reasons"].add(reason)

    tokens_by_key = {}
    anchors_by_key = {}
    attribution_anchor_tokens_by_key = {}
    token_document_frequency = Counter()
    anchor_pair_document_frequency = Counter()
    for key, message in by_key.items():
        tokens = _continuity_message_tokens(message)
        anchors = _continuity_event_anchor_pairs(message)
        tokens_by_key[key] = tokens
        anchors_by_key[key] = anchors
        attribution_anchor_tokens_by_key[key] = (
            _continuity_attribution_anchor_tokens(message)
        )
        token_document_frequency.update(tokens)
        anchor_pair_document_frequency.update(anchors)

    document_count = max(1, len(messages))
    rare_limit = max(3, int(math.ceil(document_count * 0.03)))
    common_limit = max(8, int(math.ceil(document_count * 0.12)))
    anchor_pair_limit = max(2, int(math.ceil(document_count * 0.01)))
    anchor_token_limit = max(3, int(math.ceil(document_count * 0.015)))

    def lexical_score(left_key, right_key):
        left = by_key[left_key]
        right = by_key[right_key]
        left_tokens = {
            token
            for token in tokens_by_key.get(left_key, set())
            if token_document_frequency.get(token, 0) <= common_limit
        }
        right_tokens = {
            token
            for token in tokens_by_key.get(right_key, set())
            if token_document_frequency.get(token, 0) <= common_limit
        }
        shared = left_tokens & right_tokens
        same_channel = _continuity_same_channel(left, right)

        if same_channel:
            if len(shared) < 3:
                return None
            rare_shared = sum(
                1
                for token in shared
                if token_document_frequency.get(token, 0) <= rare_limit
            )
            if rare_shared < 2:
                return None
        else:
            if len(shared) < 4:
                return None
            rare_shared = sum(
                1
                for token in shared
                if token_document_frequency.get(token, 0) <= rare_limit
            )
            if rare_shared < 3:
                return None

        overlap = len(shared) / max(
            1,
            min(len(left_tokens), len(right_tokens)),
        )
        if overlap < 0.20:
            return None

        left_summary_day = _continuity_recurring_summary_day(left)
        right_summary_day = _continuity_recurring_summary_day(right)
        if (
            same_channel
            and left_summary_day is not None
            and right_summary_day is not None
            and left_summary_day != right_summary_day
        ):
            return None

        if _event_candidate_lexical_meeting_pair_diverges(left, right):
            return None

        shared_anchor_pairs = (
            anchors_by_key.get(left_key, set())
            & anchors_by_key.get(right_key, set())
        )
        left_attribution_tokens = attribution_anchor_tokens_by_key.get(
            left_key,
            set(),
        )
        right_attribution_tokens = attribution_anchor_tokens_by_key.get(
            right_key,
            set(),
        )
        has_event_anchor = any(
            pair[0] not in left_attribution_tokens
            and pair[1] not in left_attribution_tokens
            and pair[0] not in right_attribution_tokens
            and pair[1] not in right_attribution_tokens
            and anchor_pair_document_frequency.get(pair, 0)
            <= anchor_pair_limit
            and min(
                token_document_frequency.get(pair[0], document_count),
                token_document_frequency.get(pair[1], document_count),
            )
            <= anchor_token_limit
            for pair in shared_anchor_pairs
        )
        if not has_event_anchor:
            return None

        idf_score = sum(
            math.log(
                (document_count + 1)
                / (token_document_frequency.get(token, 0) + 1)
            ) + 1.0
            for token in shared
        )
        score = idf_score + len(shared) * 0.5 + overlap * 4.0
        if score < 5.0:
            return None
        return score

    strong_components = []
    for root, keys in component_members.items():
        ordered = sorted(
            keys,
            key=lambda key: _event_candidate_sort_key(by_key[key]),
            reverse=True,
        )
        strong_components.append({
            "member_keys": ordered,
            "anchor_key": ordered[0],
            "relation_types": set(component_relations[root]["types"]),
            "relation_reasons": set(component_relations[root]["reasons"]),
        })

    strong_components.sort(
        key=lambda item: _event_candidate_sort_key(
            by_key[item["anchor_key"]]
        ),
        reverse=True,
    )

    candidates = []
    anchor_token_index = {}

    def register_candidate_anchor(candidate_index, anchor_key):
        for token in tokens_by_key.get(anchor_key, set()):
            anchor_token_index.setdefault(token, set()).add(candidate_index)

    for component in strong_components:
        anchor_key = component["anchor_key"]
        candidate_counts = Counter()
        for token in tokens_by_key.get(anchor_key, set()):
            if token_document_frequency.get(token, 0) > common_limit:
                continue
            for candidate_index in anchor_token_index.get(token, ()):
                candidate_counts[candidate_index] += 1

        best_index = None
        best_score = None
        for candidate_index, shared_count in candidate_counts.items():
            if shared_count < 3:
                continue
            score = lexical_score(
                anchor_key,
                candidates[candidate_index]["anchor_key"],
            )
            if score is None:
                continue
            if (
                best_score is None
                or score > best_score
                or (
                    score == best_score
                    and candidate_index < best_index
                )
            ):
                best_index = candidate_index
                best_score = score

        if best_index is None:
            candidates.append({
                "anchor_key": anchor_key,
                "member_keys": list(component["member_keys"]),
                "relation_types": set(component["relation_types"]),
                "relation_reasons": set(component["relation_reasons"]),
            })
            register_candidate_anchor(len(candidates) - 1, anchor_key)
        else:
            target = candidates[best_index]
            target["member_keys"].extend(component["member_keys"])
            target["relation_types"].update(component["relation_types"])
            target["relation_types"].add("lexical_candidate")
            target["relation_reasons"].update(component["relation_reasons"])
            target["relation_reasons"].add("lexical_anchor_gate")

    near_edges = [
        (left, right)
        for left, right, relation_type, _ in relation_edges
        if relation_type == "near_duplicate"
    ]

    finalized = []
    for candidate in candidates:
        member_keys = list(dict.fromkeys(candidate["member_keys"]))
        member_keys.sort(
            key=lambda key: _event_candidate_sort_key(by_key[key]),
            reverse=True,
        )
        member_set = set(member_keys)

        near_parent = {key: key for key in member_keys}

        def near_find(key):
            while near_parent[key] != key:
                near_parent[key] = near_parent[near_parent[key]]
                key = near_parent[key]
            return key

        def near_union(left, right):
            if left not in near_parent or right not in near_parent:
                return
            left_root = near_find(left)
            right_root = near_find(right)
            if left_root != right_root:
                near_parent[right_root] = left_root

        for left, right in near_edges:
            if left in member_set and right in member_set:
                near_union(left, right)

        near_groups = {}
        for key in member_keys:
            near_groups.setdefault(near_find(key), []).append(key)

        supporting_to = {}
        for keys in near_groups.values():
            if len(keys) < 2:
                continue
            retained = max(
                keys,
                key=lambda key: _event_candidate_sort_key(by_key[key]),
            )
            for key in keys:
                if key != retained:
                    supporting_to[key] = retained

        evidence_messages = [
            by_key[key]
            for key in member_keys
            if key not in supporting_to
        ]
        supporting_refs = [
            _event_candidate_ref(
                by_key[key],
                retained_as=supporting_to[key],
            )
            for key in member_keys
            if key in supporting_to
        ]
        member_refs = [
            _event_candidate_ref(by_key[key])
            for key in member_keys
        ]

        relation_types = set(candidate["relation_types"])
        if len(member_keys) == 1:
            relation = "singleton"
        elif relation_types == {"near_duplicate"}:
            relation = "near_duplicate"
        elif relation_types == {"lexical_candidate"}:
            relation = "lexical_candidate"
        elif relation_types == {"strong_source"}:
            relation = "strong_source"
        elif len(relation_types) > 1:
            relation = "mixed"
        elif "lexical_candidate" in relation_types:
            relation = "lexical_candidate"
        else:
            relation = "strong_source"

        source_identities = {
            _event_candidate_channel_identity(by_key[key])
            for key in member_keys
        }
        dates = [
            by_key[key].get("date_local")
            or by_key[key].get("date_utc")
            or ""
            for key in member_keys
        ]

        finalized.append({
            "candidate_id": "",
            "latest": max(dates) if dates else "",
            "earliest": min(dates) if dates else "",
            "messages_count": len(member_keys),
            "sources_count": len(source_identities),
            "relation": relation,
            "member_refs": member_refs,
            "evidence_messages": evidence_messages,
            "supporting_refs": supporting_refs,
            "relation_types": sorted(relation_types),
            "relation_reasons": sorted(candidate["relation_reasons"]),
        })

    finalized.sort(
        key=lambda item: (
            item["latest"],
            item["member_refs"][0]["message_key"]
            if item["member_refs"] else "",
        ),
        reverse=True,
    )
    for index, candidate in enumerate(finalized, 1):
        candidate["candidate_id"] = f"event_{index:04d}"

    assigned_counts = Counter(
        ref["message_key"]
        for candidate in finalized
        for ref in candidate["member_refs"]
    )
    input_keys = set(by_key)
    unassigned = input_keys - set(assigned_counts)
    duplicate_assignments = sum(
        count - 1
        for count in assigned_counts.values()
        if count > 1
    )
    full_evidence_count = sum(
        len(candidate["evidence_messages"])
        for candidate in finalized
    )
    supporting_count = sum(
        len(candidate["supporting_refs"])
        for candidate in finalized
    )

    if (
        unassigned
        or duplicate_assignments
        or len(assigned_counts) != len(input_keys)
        or full_evidence_count + supporting_count != len(input_keys)
    ):
        raise ValueError(
            "event candidate coverage invariant failed: "
            f"input={len(input_keys)} assigned={len(assigned_counts)} "
            f"unassigned={len(unassigned)} "
            f"duplicate_assignments={duplicate_assignments} "
            f"evidence={full_evidence_count} support={supporting_count}"
        )

    sizes = sorted(
        candidate["messages_count"]
        for candidate in finalized
    )
    if sizes:
        middle = len(sizes) // 2
        if len(sizes) % 2:
            median_size = float(sizes[middle])
        else:
            median_size = (
                sizes[middle - 1] + sizes[middle]
            ) / 2.0
        average_size = sum(sizes) / len(sizes)
        max_size = max(sizes)
    else:
        median_size = 0.0
        average_size = 0.0
        max_size = 0

    coverage = {
        "input_current_messages": len(input_keys),
        "event_candidates": len(finalized),
        "singleton_candidates": sum(
            1
            for candidate in finalized
            if candidate["messages_count"] == 1
        ),
        "full_evidence_messages": full_evidence_count,
        "near_duplicate_supporting_refs": supporting_count,
        "unassigned_messages": 0,
        "duplicate_assignments": 0,
    }
    stats = {
        "strong_groups": sum(
            1
            for candidate in finalized
            if "strong_source" in candidate["relation_types"]
        ),
        "lexical_assisted_candidates": sum(
            1
            for candidate in finalized
            if "lexical_candidate" in candidate["relation_types"]
        ),
        "max_candidate_size": max_size,
        "average_candidate_size": average_size,
        "median_candidate_size": median_size,
    }
    return {
        "candidates": finalized,
        "coverage": coverage,
        "stats": stats,
    }


def _render_event_candidate(candidate):
    lines = [
        f"## CANDIDATE {candidate['candidate_id']}",
        "",
        f"LATEST: {candidate['latest']}",
        f"EARLIEST: {candidate['earliest']}",
        f"MESSAGES: {candidate['messages_count']}",
        f"SOURCES: {candidate['sources_count']}",
        f"RELATION: {candidate['relation']}",
        "",
        "MEMBER_REFS:",
    ]
    for ref in candidate["member_refs"]:
        line = (
            f"- {ref['message_key']} | "
            f"{ref.get('channel') or ''} | "
            f"{ref.get('date_local') or ''}"
        )
        if ref.get("source_url"):
            line += f" | SOURCE_URL: {ref['source_url']}"
        lines.append(line)

    supporting_refs = candidate.get("supporting_refs") or []
    if supporting_refs:
        lines.extend(["", "SUPPORTING_REFS:"])
        for ref in supporting_refs:
            line = (
                f"- {ref['message_key']} | "
                f"{ref.get('channel') or ''} | "
                f"{ref.get('date_local') or ''}"
            )
            if ref.get("source_url"):
                line += f" | SOURCE_URL: {ref['source_url']}"
            if ref.get("retained_as"):
                line += f" | RETAINED_AS: {ref['retained_as']}"
            lines.append(line)

    lines.extend(["", "EVIDENCE:", ""])
    for message in candidate.get("evidence_messages") or []:
        lines.append(_ai_message_block(message))

    return "\n".join(lines)


def render_ai_friendly_markdown(payload):
    """
    Candidate-first AI-facing export without local summarization or ranking.

    Canonical JSON stays untouched. Textless media-only placeholders are omitted
    from this view exactly as in profile 8.4; all remaining current messages are
    covered exactly once by MEMBER_REFS, with only proven similar_message_refs
    allowed to replace repeated full evidence with SUPPORTING_REFS.
    """
    all_current_messages = [
        *list(payload.get("news_messages") or []),
        *list(payload.get("operational_messages") or []),
    ]
    current_messages = [
        item
        for item in all_current_messages
        if isinstance(item, dict)
        and not _is_textless_media_only_for_ai(item)
    ]
    omitted_media_only = len(all_current_messages) - len(current_messages)

    candidate_layer = build_event_candidates(current_messages)
    candidates = candidate_layer["candidates"]
    coverage = candidate_layer["coverage"]

    dates = [
        item.get("date_local") or item.get("date_utc")
        for item in all_current_messages
        if isinstance(item, dict)
        and (item.get("date_local") or item.get("date_utc"))
    ]
    interval = ""
    if dates:
        interval = f"{min(dates)} — {max(dates)}"

    lines = [
        "# TelegramNewsAI — материал для ИИ",
        "",
        f"DIGEST_PROFILE: {DIGEST_PROFILE_VERSION}",
        f"INPUT_CURRENT_MESSAGES: {coverage['input_current_messages']}",
        f"EVENT_CANDIDATES: {coverage['event_candidates']}",
        f"SINGLETON_CANDIDATES: {coverage['singleton_candidates']}",
        f"FULL_EVIDENCE_MESSAGES: {coverage['full_evidence_messages']}",
        (
            "NEAR_DUPLICATE_SUPPORTING_REFS: "
            f"{coverage['near_duplicate_supporting_refs']}"
        ),
        f"UNASSIGNED_MESSAGES: {coverage['unassigned_messages']}",
        f"DUPLICATE_ASSIGNMENTS: {coverage['duplicate_assignments']}",
        f"OMITTED_MEDIA_ONLY: {omitted_media_only}",
    ]
    if interval:
        lines.append(f"LOCAL_INTERVAL: {interval}")

    lines.extend([
        "",
        "## Задача",
        "",
        CANDIDATE_GUIDANCE,
        "",
        DIGEST_REQUEST.strip(),
        "",
        "## Кандидаты событий",
        "",
        "Candidates идут от новых к старым по LATEST. Локальный слой не создаёт заголовки, summary, важность или truth-оценку.",
        "",
    ])

    for candidate in candidates:
        lines.append(_render_event_candidate(candidate))

    continuity = payload.get("continuity_context") or {}
    continuity_messages = continuity.get("messages") or []
    if continuity_messages:
        lines.extend([
            "",
            "## Предыстория вне выбранного периода",
            "",
            "Используй только для понимания развития текущих сюжетов; это не текущие новости.",
            "",
        ])
        for item in continuity_messages:
            if not isinstance(item, dict):
                continue
            context_message = item.get("context_message")
            if isinstance(context_message, dict):
                lines.append(
                    _ai_message_block(
                        context_message,
                        label="CONTEXT",
                        related_current_refs=item.get("related_current_message_refs"),
                    )
                )

    changes = payload.get("changes_since_previous_digest") or {}
    outside = changes.get("outside_period_changes") or []
    if outside:
        lines.extend([
            "",
            "## Содержательные изменения вне периода",
            "",
            "Это дополнительный контекст изменений, а не расширение временного окна основного дайджеста.",
            "",
        ])
        for message in outside:
            if isinstance(message, dict):
                lines.append(_ai_message_block(message, label="OUTSIDE_CHANGE"))

    return "\n".join(lines).rstrip() + "\n"

def write_ai_friendly_export(payload, destination=None):
    destination = Path(destination or AI_LATEST_FILE)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        render_ai_friendly_markdown(payload),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def _v4_build_changes_block(
    news_messages,
    operational_messages,
    previous_digest_utc,
):
    """
    Инкрементальный блок без повторного хранения текста.
    Полный объект находится один раз в news_messages /
    operational_messages и адресуется через message_key.
    """
    comparison_available = bool(
        previous_digest_utc
    )

    if not comparison_available:
        return {
            "comparison_available": False,
            "baseline_utc": None,
            "new_message_refs": [],
            "edited_message_refs": [],
            "new_operational_refs": [],
            "edited_operational_refs": [],
            "note": (
                "Нет надёжной точки сравнения. "
                "Используй полный news_messages как первый базовый дайджест."
            ),
        }

    new_messages = [
        m for m in news_messages
        if m.get("change_status")
        == "new_since_previous_digest"
    ]

    edited_messages = [
        m for m in news_messages
        if m.get("change_status")
        == "edited_since_previous_digest"
    ]

    new_operational = [
        m for m in operational_messages
        if m.get("change_status")
        == "new_since_previous_digest"
    ]

    edited_operational = [
        m for m in operational_messages
        if m.get("change_status")
        == "edited_since_previous_digest"
    ]

    return {
        "comparison_available": True,
        "baseline_utc": previous_digest_utc,
        "counts": {
            "new_news": len(new_messages),
            "edited_news": len(edited_messages),
            "new_operational": len(new_operational),
            "edited_operational": len(edited_operational),
        },
        "new_message_refs": [
            make_compact_message_ref(m)
            for m in new_messages
        ],
        "edited_message_refs": [
            make_compact_message_ref(m)
            for m in edited_messages
        ],
        "new_operational_refs": [
            make_compact_message_ref(m)
            for m in new_operational
        ],
        "edited_operational_refs": [
            make_compact_message_ref(m)
            for m in edited_operational
        ],
        "note": (
            "Ссылки new/edited относятся к сообщениям выбранного периода: "
            "их полный текст хранится в news_messages/operational_messages и "
            "находится по message_key. Контекст вне периода, если он есть, "
            "хранится полными объектами в outside_period_changes."
        ),
    }


def _v4_save_output(
    raw_messages,
    news_messages,
    operational_messages,
    exact_count,
    near_count,
    hours,
    channels,
    sync_stats,
    previous_run_utc,
    previous_digest_source,
    related_groups,
    settings,
):
    now = datetime.now().astimezone()
    stamp = now.strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    ensure_dirs()

    change_summary = (
        calculate_change_summary(
            raw_messages
        )
    )

    changes_block = build_changes_block(
        news_messages,
        operational_messages,
        previous_run_utc,
    )

    outside_period_changes = [
        message
        for message in raw_messages
        if message.get("in_selected_period", True) is False
        and message.get("change_status") in {
            "new_since_previous_digest",
            "edited_since_previous_digest",
            "unavailable_since_previous_digest",
        }
    ]

    period_start_utc = (
        now.astimezone(timezone.utc)
        - timedelta(hours=max(1.0, float(hours)))
    )
    continuity_prior_messages = (
        load_recent_continuity_messages(
            channels,
            period_start_utc,
            reference_utc=now,
        )
        if previous_run_utc
        else []
    )
    continuity_context = build_continuity_context(
        news_messages,
        continuity_prior_messages,
    )
    changes_block["outside_period_changes"] = [
        prepare_message_for_ai(message)
        for message in outside_period_changes
    ]
    changes_block["outside_period_change_count"] = len(
        outside_period_changes
    )

    exported_news_messages = [
        prepare_message_for_ai(message)
        for message in news_messages
    ]
    exported_operational_messages = [
        prepare_message_for_ai(message)
        for message in operational_messages
    ]

    payload = {
        "meta": {
            "collector_version": APP_VERSION,
            "created_local": now.isoformat(
                timespec="seconds"
            ),
            "created_utc": iso_utc(
                now.astimezone(timezone.utc)
            ),
            "hours": hours,

            "previous_digest_utc": (
                previous_run_utc
            ),
            "previous_digest_source": (
                previous_digest_source
            ),
            "comparison_available": bool(
                previous_run_utc
            ),
            "selection_fingerprint": (
                selection_fingerprint(channels)
            ),

            "channels_count": len(
                channels
            ),
            "channels": [
                {
                    "name": ch["name"],
                    "username": ch.get(
                        "username"
                    ),
                }
                for ch in channels
            ],

            "database": {
                "file": "news.db",
                "persistent_history": True,
                "retention_days": int(
                    settings.get(
                        "database_retention_days",
                        90,
                    )
                ),
                "max_size_mb": int(
                    settings.get(
                        "max_database_mb",
                        500,
                    )
                ),
                "new_messages_saved_this_run": (
                    sync_stats[
                        "new_messages_saved"
                    ]
                ),
                "content_changed_messages_this_run": (
                    sync_stats[
                        "content_changed_messages_refreshed"
                    ]
                ),
                "metrics_changed_messages_this_run": (
                    sync_stats[
                        "metrics_changed_messages_refreshed"
                    ]
                ),
                "migrated_messages_this_run": (
                    sync_stats[
                        "migrated_messages"
                    ]
                ),
                "telegram_messages_scanned_this_run": (
                    sync_stats[
                        "telegram_messages_scanned"
                    ]
                ),
            },

            "schema_version": EXPORT_SCHEMA_VERSION,
            "artifact_type": "telegram_news_digest",
            "history_completeness": sync_stats.get("history_completeness", {}),
            "sync_quality": {
                "complete": (
                    sync_stats[
                        "failed_channels"
                    ] == 0 and sync_stats.get("history_completeness", {}).get("complete", False)
                ),
                "successful_channels": (
                    sync_stats[
                        "successful_channels"
                    ]
                ),
                "failed_channels": (
                    sync_stats[
                        "failed_channels"
                    ]
                ),
                "failed": [
                    {
                        "channel": item[
                            "channel"
                        ],
                        "error": item.get(
                            "error"
                        ),
                    }
                    for item in sync_stats[
                        "channel_results"
                    ]
                    if item.get("status")
                    != "ok"
                ],
            },

            "raw_messages_in_period": sum(
                1
                for message in raw_messages
                if message.get("in_selected_period", True)
            ),
            "change_context_messages_outside_period": len(
                outside_period_changes
            ),
            "news_messages_after_cleanup": len(
                news_messages
            ),
            "operational_messages_separated": len(
                operational_messages
            ),
            "exact_duplicates_removed": (
                exact_count
            ),
            "near_duplicates_removed": (
                near_count
            ),
            "related_groups_count": len(
                related_groups
            ),
            "continuity_context_messages": (
                continuity_context["messages_count"]
            ),

            "change_summary": (
                change_summary
            ),

            "enriched_fields": [
                "edit_date",
                "replies",
                "reactions",
                "media",
                "album_id",
                "forwarded_from",
                "external_urls",
                "canonical_urls",
                "origin_key",
                "related_group_id",
                "change_status",
            ],

            "self_diagnostics": (
                sync_stats.get(
                    "self_diagnostics",
                    {}
                )
            ),

            "digest_profile_version": DIGEST_PROFILE_VERSION,
            "raw_text_representation": (
                "Если raw_text отсутствует и raw_text_available не равно false, "
                "исходный текст совпадает с text после удаления краевых пробелов и "
                "не повторён. raw_text_available=false означает, что исходный текст "
                "до очистки не был сохранён старой версией. Пустые необязательные "
                "поля и обычное availability=available также не повторяются."
            ),
            "usage_hint": (
                "Это полный канонический машинный экспорт. Для внешнего ИИ рядом "
                "создаётся candidate-first ДАЙДЖЕСТ_ДЛЯ_ИИ.md без предварительной "
                "суммаризации: каждое содержательное текущее сообщение трассируется "
                "через candidate, а доказанные near-duplicates могут быть представлены "
                "SUPPORTING_REFS вместо повторного полного текста; чистые media-only "
                "без подписи остаются только в каноническом JSON. "
                "Редакционная инструкция "
                "также остаётся в recommended_digest_request. Программа не отправляет "
                "файлы во внешние сервисы автоматически."
            ),
            "recommended_digest_request": (
                DIGEST_REQUEST
            ),
            "content_use_notice": (
                "Файл создан локально. Программа сама не передаёт Telegram-"
                "контент внешним AI/ML-сервисам. Дальнейшее использование "
                "должно соответствовать правилам Telegram, правам авторов "
                "и применимому законодательству."
            ),
        },

        "changes_since_previous_digest": (
            changes_block
        ),

        "continuity_context": (
            continuity_context
        ),

        "related_message_groups": (
            related_groups
        ),

        "news_messages": exported_news_messages,
        "operational_messages": (
            exported_operational_messages
        ),
    }

    archive_path = (
        ARCHIVE_DIR
        / f"ДАЙДЖЕСТ_{stamp}.json"
    )

    # Сначала пишем временный файл,
    # потом атомарно заменяем "последний".
    temp_latest = (
        OUTPUT_DIR
        / "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.tmp"
    )

    temp_latest.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    os.replace(
        temp_latest,
        LATEST_FILE,
    )

    shutil.copy2(
        LATEST_FILE,
        archive_path,
    )

    try:
        write_ai_friendly_export(
            payload,
            AI_LATEST_FILE,
        )
    except Exception as e:
        log_error(
            "AI-friendly export failed (canonical JSON saved): "
            + str(e)
        )

    raw_path = None

    if settings.get(
        "save_raw_backup",
        True,
    ):
        raw_path = (
            RAW_DIR
            / f"backup_raw_{stamp}.json"
        )

        raw_path.write_text(
            json.dumps(
                raw_messages,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return (
        LATEST_FILE,
        archive_path,
        raw_path,
        change_summary,
    )


def cleanup_old_files(settings):
    now = time.time()

    archive_days = max(
        1,
        int(
            settings.get(
                "archive_retention_days",
                30,
            )
        ),
    )

    raw_days = max(
        1,
        int(
            settings.get(
                "raw_retention_days",
                7,
            )
        ),
    )

    log_days = max(
        1,
        int(
            settings.get(
                "log_retention_days",
                14,
            )
        ),
    )

    archive_cutoff = (
        now - archive_days * 86400
    )
    raw_cutoff = (
        now - raw_days * 86400
    )
    log_cutoff = (
        now - log_days * 86400
    )

    removed = 0

    # Датированные готовые дайджесты.
    if ARCHIVE_DIR.exists():
        for path in ARCHIVE_DIR.glob("*.json"):
            try:
                if path.stat().st_mtime < archive_cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                pass

    # Архив тематических поисков хранится столько же, сколько готовые дайджесты.
    if SEARCH_ARCHIVE_DIR.exists():
        for path in SEARCH_ARCHIVE_DIR.glob("ПОИСК_*.json"):
            try:
                if path.stat().st_mtime < archive_cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                pass

    # Сырые копии живут меньше.
    if RAW_DIR.exists():
        for path in RAW_DIR.glob("backup_raw_*.json"):
            try:
                if path.stat().st_mtime < raw_cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                pass

    # Логи.
    if LOG_DIR.exists():
        for path in LOG_DIR.glob("collector_*.log"):
            try:
                if path.stat().st_mtime < log_cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                pass

    return removed


def database_file_size_mb():
    total = 0

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_FILE) + suffix)
        try:
            if p.exists():
                total += p.stat().st_size
        except Exception:
            pass

    return total / 1024 / 1024


def compact_database(conn):
    """
    Реально возвращает свободное место ОС:
    checkpoint WAL + VACUUM.
    """
    conn.commit()

    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    conn.execute("VACUUM")
    conn.commit()

    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass


def prune_database_by_age(conn, settings):
    retention_days = max(1, int(settings.get('database_retention_days', 90)))
    cutoff = iso_utc(utc_now() - timedelta(days=retention_days))
    # Snapshot valid coverage within the same transaction as deletion. The
    # generic delete trigger still invalidates coverage for arbitrary deletions.
    conn.execute('SAVEPOINT age_prune')
    try:
        coverage = conn.execute('SELECT channel_id, history_coverage_utc FROM channels WHERE history_coverage_utc IS NOT NULL').fetchall()
        cursor = conn.execute('DELETE FROM messages WHERE date_utc < ?', (cutoff,))
        deleted = cursor.rowcount
        conn.executemany('UPDATE channels SET history_coverage_utc=? WHERE channel_id=?',
                         [(max(row['history_coverage_utc'], cutoff), row['channel_id']) for row in coverage])
        conn.execute('RELEASE age_prune')
        conn.commit()
    except BaseException:
        conn.execute('ROLLBACK TO age_prune')
        conn.execute('RELEASE age_prune')
        raise
    return retention_days, deleted


def enforce_database_size_limit(conn, settings):
    """
    Жёсткий предохранитель:
    если news.db вместе с WAL/SHM превышает max_database_mb,
    оставляем самые свежие сообщения, целясь чуть ниже лимита,
    затем VACUUM. При необходимости повторяем.
    """
    max_mb = max(
        50,
        int(
            settings.get(
                "max_database_mb",
                500,
            )
        ),
    )

    target_mb = int(
        settings.get(
            "database_target_mb_after_prune",
            max(50, int(max_mb * 0.9)),
        )
    )

    target_mb = max(
        25,
        min(target_mb, max_mb - 1),
    )

    # Сначала схлопываем WAL, чтобы оценка размера была честнее.
    try:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass

    size_before = database_file_size_mb()

    if size_before <= max_mb:
        return {
            "limit_mb": max_mb,
            "target_mb": target_mb,
            "size_before_mb": round(size_before, 2),
            "size_after_mb": round(size_before, 2),
            "messages_deleted": 0,
            "triggered": False,
        }

    deleted_total = 0

    for _ in range(4):
        current_size = database_file_size_mb()

        if current_size <= max_mb:
            break

        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages"
        ).fetchone()

        total_rows = int(row["c"] or 0)

        if total_rows <= 1000:
            # Слишком мало строк, дальше удалять рискованно.
            break

        # Оцениваем, сколько строк оставить, чтобы после VACUUM
        # оказаться около target_mb. Добавляем запас ~5%.
        keep_fraction = min(
            0.95,
            max(
                0.10,
                (target_mb / max(current_size, 1.0)) * 0.95,
            ),
        )

        keep_rows = max(
            1000,
            int(total_rows * keep_fraction),
        )

        to_delete = max(
            1,
            total_rows - keep_rows,
        )

        cursor = conn.execute(
            """
            DELETE FROM messages
            WHERE rowid IN (
                SELECT rowid
                FROM messages
                ORDER BY date_utc ASC
                LIMIT ?
            )
            """,
            (to_delete,),
        )

        conn.commit()

        deleted_now = cursor.rowcount

        deleted_total += deleted_now

        if deleted_now <= 0:
            break

        compact_database(conn)

    size_after = database_file_size_mb()

    return {
        "limit_mb": max_mb,
        "target_mb": target_mb,
        "size_before_mb": round(size_before, 2),
        "size_after_mb": round(size_after, 2),
        "messages_deleted": deleted_total,
        "triggered": True,
    }


def get_app_meta(conn, key):
    row = conn.execute(
        """
        SELECT value
        FROM app_meta
        WHERE key = ?
        """,
        (key,),
    ).fetchone()

    return row["value"] if row else None


def set_app_meta(conn, key, value):
    conn.execute(
        """
        INSERT INTO app_meta (
            key,
            value
        )
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET
            value = excluded.value
        """,
        (
            key,
            str(value),
        ),
    )
    conn.commit()


def run_quick_check_if_due(conn, settings):
    interval_days = max(
        1,
        int(
            settings.get(
                "sqlite_quick_check_interval_days",
                7,
            )
        ),
    )

    now = utc_now()
    last_checked = parse_dt(
        get_app_meta(
            conn,
            "quick_check_utc",
        )
    )

    due = (
        last_checked is None
        or (
            now - last_checked
        ).total_seconds()
        >= interval_days * 86400
    )

    if due:
        try:
            rows = conn.execute(
                "PRAGMA quick_check"
            ).fetchall()

            messages = [
                str(row[0])
                for row in rows
            ]

            ok = (
                len(messages) == 1
                and messages[0].lower()
                == "ok"
            )

            result_text = (
                "ok"
                if ok
                else "; ".join(
                    messages[:10]
                )
            )

        except Exception as e:
            ok = False
            result_text = (
                f"{type(e).__name__}: {e}"
            )

        checked_at = iso_utc(now)

        set_app_meta(
            conn,
            "quick_check_utc",
            checked_at,
        )
        set_app_meta(
            conn,
            "quick_check_result",
            result_text,
        )

        return {
            "quick_check_ran": True,
            "quick_check_ok": ok,
            "quick_check_result": result_text,
            "quick_check_utc": checked_at,
            "interval_days": interval_days,
        }

    previous_result = get_app_meta(
        conn,
        "quick_check_result",
    )

    return {
        "quick_check_ran": False,
        "quick_check_ok": (
            previous_result == "ok"
            if previous_result is not None
            else None
        ),
        "quick_check_result": (
            previous_result
            or "not_run_yet"
        ),
        "quick_check_utc": (
            iso_utc(last_checked)
            if last_checked
            else None
        ),
        "interval_days": interval_days,
    }


def optimize_database(conn):
    try:
        conn.execute("PRAGMA optimize")
        conn.commit()
        return {
            "optimize_ok": True,
            "optimize_error": None,
        }
    except Exception as e:
        return {
            "optimize_ok": False,
            "optimize_error": (
                f"{type(e).__name__}: {e}"
            ),
        }


def _v4_maintain_database(conn, settings):
    """
    1) удаляет сообщения старше database_retention_days;
    2) реально уплотняет файл после возрастной очистки;
    3) затем применяет жёсткий лимит размера news.db.
    """
    retention_days, deleted_by_age = prune_database_by_age(
        conn,
        settings,
    )

    if deleted_by_age > 0:
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        free = conn.execute('PRAGMA freelist_count').fetchone()[0]
        size = conn.execute('PRAGMA page_size').fetchone()[0]
        if free * size >= 16 * 1024 * 1024 and free >= pages * 0.2:
            compact_database(conn)

    size_result = enforce_database_size_limit(
        conn,
        settings,
    )

    health = run_quick_check_if_due(
        conn,
        settings,
    )

    result = {
        "retention_days": retention_days,
        "deleted_by_age": deleted_by_age,
        **size_result,
        **health,
    }

    return result


SEARCH_STOPWORDS = {
    # Русские служебные слова / формулировки естественного вопроса.
    "что", "как", "где", "когда", "кто", "почему", "зачем", "какой",
    "какая", "какие", "какое", "про", "по", "с", "со", "в", "во", "на",
    "за", "из", "от", "до", "для", "и", "или", "а", "но", "же", "ли",
    "это", "этой", "этот", "эти", "там", "тут", "сейчас", "вообще",
    "последний", "последние", "последних", "день", "дня", "дней",
    "неделя", "недели", "недель", "месяц", "месяца", "месяцев",
    "сегодня", "вчера", "недавно", "тема", "темой", "новости", "новость",
    # Украинские.
    "що", "як", "де", "коли", "хто", "чому", "навіщо", "який", "яка",
    "які", "про", "по", "з", "зі", "із", "у", "в", "на", "за", "до",
    "для", "і", "або", "але", "це", "цей", "ця", "ці", "зараз",
    "останній", "останні", "останніх", "день", "дні", "днів", "тиждень",
    "тижні", "місяць", "місяця", "місяців", "сьогодні", "вчора", "новини",
}


def normalize_search_token(token):
    token = (token or "").casefold().replace("ё", "е")
    return token.strip("_-")








def fts5_is_available(conn):
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            ("fts5_available",),
        ).fetchone()
        if row and str(row["value"]) == "1":
            return True

        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        return bool(table)
    except Exception:
        return False


def run_fts_search(conn, cutoff_iso, terms, settings, limit, operator="AND", channel_ids=None):
    if not terms or not fts5_is_available(conn):
        return None

    query = build_fts_query(
        terms,
        settings,
        operator=operator,
    )

    channel_sql = ""
    channel_params = []

    if channel_ids:
        ids = [
            int(x)
            for x in channel_ids
        ]
        placeholders = ",".join(
            "?" for _ in ids
        )
        channel_sql = (
            f" AND m.channel_id IN ({placeholders})"
        )
        channel_params = ids

    try:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH ?
              AND m.date_utc >= ?
              {channel_sql}
            """,
            (
                query,
                cutoff_iso,
                *channel_params,
            ),
        ).fetchone()

        rows = conn.execute(
            f"""
            SELECT m.*, bm25(messages_fts) AS search_rank
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH ?
              AND m.date_utc >= ?
              {channel_sql}
            ORDER BY search_rank ASC, m.date_utc DESC
            LIMIT ?
            """,
            (
                query,
                cutoff_iso,
                *channel_params,
                int(limit),
            ),
        ).fetchall()

        return {
            "rows": rows,
            "total": int(total_row["c"] or 0),
            "method": f"fts5_{operator.lower()}",
            "query": query,
        }
    except Exception as e:
        log_error(f"FTS5 search failed: {type(e).__name__}: {e}")
        return None


def run_like_search(conn, cutoff_iso, terms, settings, limit, operator="AND", channel_ids=None):
    if not terms:
        return {
            "rows": [],
            "total": 0,
            "method": "like_none",
            "query": None,
        }

    term_clauses = []
    params = []

    # LIKE — только аварийный резерв для сборок SQLite без FTS5, но он
    # повторяет ту же логику вариантов и морфологических префиксов.
    for token in terms:
        variant_clauses = []
        for variant in term_variants(token, settings):
            folded = search_fold(str(variant))
            if ' ' in folded:
                needle = folded
            else:
                needle = search_stem_prefix(folded)
            variant_clauses.append("unicode_fold(text) LIKE ?")
            params.append(f"%{needle}%")
        term_clauses.append('(' + ' OR '.join(variant_clauses) + ')')

    joiner = " AND " if operator == "AND" else " OR "
    where = joiner.join(term_clauses)

    channel_sql = ""
    channel_params = []

    if channel_ids:
        ids = [int(x) for x in channel_ids]
        placeholders = ",".join("?" for _ in ids)
        channel_sql = f" AND channel_id IN ({placeholders})"
        channel_params = ids

    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM messages
        WHERE date_utc >= ?
          AND ({where})
          {channel_sql}
        """,
        (
            cutoff_iso,
            *params,
            *channel_params,
        ),
    ).fetchone()

    rows = conn.execute(
        f"""
        SELECT *, 0.0 AS search_rank
        FROM messages
        WHERE date_utc >= ?
          AND ({where})
          {channel_sql}
        ORDER BY date_utc DESC
        LIMIT ?
        """,
        (
            cutoff_iso,
            *params,
            *channel_params,
            int(limit),
        ),
    ).fetchall()

    return {
        "rows": rows,
        "total": int(total_row["c"] or 0),
        "method": f"like_{operator.lower()}",
        "query": where,
    }



def database_coverage(conn, channel_ids=None):
    if channel_ids:
        ids = [
            int(x)
            for x in channel_ids
        ]
        placeholders = ",".join(
            "?" for _ in ids
        )
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS c,
                MIN(date_utc) AS min_date,
                MAX(date_utc) AS max_date,
                COUNT(DISTINCT channel_id) AS channels
            FROM messages
            WHERE channel_id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS c,
                MIN(date_utc) AS min_date,
                MAX(date_utc) AS max_date,
                COUNT(DISTINCT channel_id) AS channels
            FROM messages
            """
        ).fetchone()

    return {
        "messages": int(row["c"] or 0),
        "channels": int(row["channels"] or 0),
        "earliest_utc": row["min_date"],
        "latest_utc": row["max_date"],
    }


def safe_filename_fragment(value, max_len=48):
    value = re.sub(r'[<>:"/\\|?*]+', " ", value or "")
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"_+", "_", value)
    value = value.strip("._ ")
    if not value:
        value = "тема"
    return value[:max_len].rstrip("._ ")


def build_search_digest_instruction(question, effective_days, search_result):
    return (
        "Подготовь профессиональный тематический обзор по вопросу: "
        f"«{question}». Период: последние {effective_days} дней. Если пользователь после загрузки пишет только «дайджест», "
        "отвечай именно по этой теме, а не по всей повестке. search_results содержат основные найденные публикации; "
        "related_context используй только как связанный контекст; related_message_groups помогают распознавать перепечатки "
        "и общий источник. Совпадения, найденные по смыслу, включай только после проверки их фактической связи с вопросом. "
        "Если история за период неполна, кратко предупреди об этом человеческим языком без технических деталей. "
        + EDITORIAL_PRINCIPLES +
        SOURCE_RULES +
        "Начни с 1–3 предложений о состоянии темы в целом, затем раскрой события по естественной хронологии или смысловым "
        "подтемам. Покажи ключевые изменения, важные версии и расхождения, не создавая искусственную многоуровневую "
        "структуру при малом количестве материала. Если из данных естественно следуют открытые вопросы, кратко укажи, что "
        "важно отслеживать дальше. "
    )


def _v4_save_search_output(conn, question, days, settings, db_maintenance, channels=None, history_status=None, search_result=None):
    channel_ids = [
        int(ch["id"])
        for ch in (channels or [])
    ]

    result = search_result or search_database(
        conn,
        question,
        days,
        settings,
        channel_ids=channel_ids or None,
    )

    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    fragment = safe_filename_fragment(question)

    archive_path = (
        SEARCH_ARCHIVE_DIR
        / f"ПОИСК_{fragment}_{result['effective_days']}д_{stamp}.json"
    )

    coverage = database_coverage(conn, channel_ids=channel_ids or None)

    payload = {
        "meta": {
            "artifact_type": "telegram_topic_search_digest",
            "collector_version": APP_VERSION,
            "schema_version": EXPORT_SCHEMA_VERSION,
            "digest_profile_version": DIGEST_PROFILE_VERSION,
            "created_local": now.isoformat(timespec="seconds"),
            "created_utc": iso_utc(now.astimezone(timezone.utc)),
            "search_intent": {
                "user_question": result["question"],
                "requested_days": result["requested_days"],
                "effective_days": result["effective_days"],
                "database_retention_days": result["database_retention_days"],
                "cutoff_utc": result["cutoff_utc"],
            },
            "search_engine": {
                "semantic": result.get("semantic", {}),
                "method": result["method"],
                "lexical_terms": result["terms"],
                "phrase_variants": result.get("phrase_variants", []),
                "minimum_term_matches": result.get("minimum_term_matches"),
                "broadened_to_or": result["broadened_to_or"],
                "partial_raw_candidates": result.get("partial_raw_candidates", 0),
                "rejected_partial_candidates": result.get("rejected_partial_candidates", 0),
                "partial_pool_truncated": result.get("partial_pool_truncated", False),
                "total_direct_hits": result["total_direct_hits"],
                "total_lexical_hits": result.get("total_lexical_hits", result["total_direct_hits"]),
                "total_semantic_hits": result.get("total_semantic_hits", 0),
                "exported_direct_hits": len(result["direct_results"]),
                "truncated": result["truncated"],
                "related_context_messages": len(result["related_context"]),
                "fts5_available": fts5_is_available(conn),
            },
            "database_coverage": coverage,
            "history_completeness": (
                history_status
                or {
                    "complete": False,
                    "warning": (
                        "Полнота истории не проверялась."
                    ),
                }
            ),
            "selected_channels": [
                {
                    "name": ch["name"],
                    "username": ch.get("username"),
                }
                for ch in (channels or [])
            ],
            "database_integrity": {
                "quick_check_ok": db_maintenance.get("quick_check_ok"),
                "quick_check_result": db_maintenance.get("quick_check_result"),
            },
            "raw_text_representation": (
                "Если raw_text отсутствует и raw_text_available не равно false, "
                "исходный текст совпадает с text после удаления краевых пробелов и "
                "не повторён. raw_text_available=false означает, что исходный текст "
                "до очистки не был сохранён старой версией. Пустые необязательные "
                "поля и обычное availability=available также не повторяются."
            ),
            "usage_hint": (
                "Локальный структурированный JSON-экспорт для пользовательского "
                "анализа в выбранном ИИ-ассистенте, например ChatGPT. Вопрос, "
                "период и готовая инструкция уже записаны внутри файла; программа "
                "не отправляет его во внешние сервисы автоматически."
            ),
            "recommended_digest_request": build_search_digest_instruction(
                result["question"],
                result["effective_days"],
                result,
            ),
            "content_use_notice": (
                "Программа сама не передаёт Telegram-контент внешним AI/ML-"
                "сервисам. Дальнейшее использование файла должно соответствовать "
                "правилам Telegram, правам авторов и применимому законодательству."
            ),
        },
        "search_overview": {
            "question": result["question"],
            "period_days": result["effective_days"],
            "total_direct_hits": result["total_direct_hits"],
            "exported_direct_hits": len(result["direct_results"]),
            "related_context_messages": len(result["related_context"]),
            "earliest_direct_match_utc": result["earliest_direct_match_utc"],
            "latest_direct_match_utc": result["latest_direct_match_utc"],
            "channels_with_direct_hits": result["channels_with_direct_hits"],
        },
        "search_results": [
            prepare_message_for_ai(message)
            for message in result["direct_results"]
        ],
        "related_context": [
            prepare_message_for_ai(message)
            for message in result["related_context"]
        ],
        "related_message_groups": result["related_message_groups"],
    }

    temp = OUTPUT_DIR / "ПОИСК_ПОСЛЕДНИЙ.tmp"
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temp, SEARCH_LATEST_FILE)
    shutil.copy2(SEARCH_LATEST_FILE, archive_path)

    return SEARCH_LATEST_FILE, archive_path, payload


async def run_history_search_mode(
    client,
    conn,
    channels,
    settings,
    db_maintenance,
):
    selected_ids = [
        int(ch["id"])
        for ch in channels
    ]

    coverage = database_coverage(
        conn,
        channel_ids=selected_ids,
    )

    print("\n" + "=" * 58)
    print("БЫСТРЫЙ ПОИСК ПО ЛОКАЛЬНОЙ БАЗЕ")
    print("=" * 58)

    print(
        f"Текущая база выбранных каналов: "
        f"{coverage['messages']} сообщений / "
        f"{coverage['channels']} каналов"
    )

    print(
        "\nНапишите вопрос обычными словами. Примеры:\n"
        "  что с колл центрами\n"
        "  квантовые батареи\n"
        "  премьера сериала дата выхода\n"
        "  что происходило с налогами ФОП"
    )
    print("0 — Назад в главное меню.")

    question = input("\nЧто ищем: ").strip()

    if is_back_command(question):
        print("Поиск отменён.")
        return None

    if not question:
        print("Пустой запрос — поиск отменён.")
        return None

    default_days = max(
        1,
        int(
            settings.get(
                "search_default_days",
                30,
            )
        ),
    )

    retention_days = max(
        1,
        int(
            settings.get(
                "database_retention_days",
                90,
            )
        ),
    )

    print("0 — Назад в главное меню.")
    raw_days = input(
        f"Период, дней [{default_days}; максимум {retention_days}]: "
    ).strip()

    if is_back_command(raw_days):
        print("Поиск отменён.")
        return None

    try:
        days = (
            int(raw_days)
            if raw_days
            else default_days
        )
    except ValueError:
        days = default_days

    days = max(1, days)

    if days > retention_days:
        print(
            f"Хранится максимум {retention_days} дней; "
            f"будет использовано {retention_days} дней."
        )

    history_status = local_history_status(conn, channels, days, settings)
    print(f"Покрытие периода: {history_status['complete_channels']}/{len(channels)}; "
          f"нуждаются в обновлении: {len(history_status['stale_channel_ids'])}.")
    if history_status['complete']:
        print(f"База свежая в пределах {history_status['freshness_minutes']:g} минут; поиск по локальному снимку.")
    else:
        print(f'Первая загрузка истории за {days} дней может быть долгой; «н» — поиск по имеющейся базе.')
        update = input(
            'Обновить устаревшие каналы и заполнить недостающий период? '
            '[Д/н; 0=назад]: '
        ).strip().casefold()
        if is_back_command(update):
            print('Поиск отменён.')
            return None
        if update in ('', 'д', 'да', 'y', 'yes'):
            own_client, _ = await ensure_telegram_client(client, None, settings)
            try:
                resolved = await resolve_channels(own_client, skip_menu=True)
                if {ch['id'] for ch in resolved} != {ch['id'] for ch in channels}:
                    raise RuntimeError('Часть выбранных каналов недоступна. Поиск отменён; проверьте список каналов.')
                history_status = await ensure_history_for_search(
                    own_client, conn, resolved, days, settings, skip_fresh=True)
            finally:
                if client is None:
                    await own_client.disconnect()

    search_result = search_database(
        conn,
        question,
        days,
        settings,
        channel_ids=selected_ids,
    )
    if search_result["truncated"]:
        print(
            f"Найдено {search_result['total_direct_hits']}; "
            f"в обычную выгрузку войдут {len(search_result['direct_results'])} "
            "наиболее релевантных публикаций."
        )
        export_all = input(
            "Выгрузить все результаты? [д/Н; 0=назад]: "
        ).strip().casefold()
        if is_back_command(export_all):
            print("Поиск отменён.")
            return None
        if export_all in ("д", "да", "y", "yes"):
            search_result = search_database(
                conn,
                question,
                days,
                settings,
                channel_ids=selected_ids,
                limit_override=0,
            )

    latest_path, archive_path, payload = save_search_output(
        conn,
        question,
        days,
        settings,
        db_maintenance,
        channels=channels,
        history_status=history_status,
        search_result=search_result,
    )

    overview = payload["search_overview"]
    engine = payload["meta"]["search_engine"]

    print("\n" + "=" * 58)
    print("ПОИСК ГОТОВ")
    print("=" * 58)

    print(
        f"Вопрос: {overview['question']}"
    )
    print(
        f"Период: {overview['period_days']} дней"
    )
    print(
        "Источник результатов: локальная база"
    )

    print("Проверка периода: " + ("выполнена" if history_status.get("complete") else "неполная или не выполнялась"))

    print(
        f"Релевантных результатов: "
        f"{overview['total_direct_hits']}"
    )
    print(
        f"Экспортировано: "
        f"{overview['exported_direct_hits']}"
    )
    print(
        f"Связанного контекста: "
        f"{overview['related_context_messages']}"
    )
    print(
        f"Метод: {engine['method']}"
    )

    if engine["broadened_to_or"]:
        print(
            "ДОПОЛНЕНИЕ: строгих совпадений было мало; добавлены только "
            f"частичные результаты с покрытием не ниже {engine.get('minimum_term_matches')}/"
            f"{len(engine.get('lexical_terms') or [])}. Совпадения по одному слову отброшены."
        )

    if engine["truncated"]:
        print(
            "ВНИМАНИЕ: совпадений больше лимита экспорта; "
            "в файл попали наиболее релевантные результаты."
        )

    print("\nСТРУКТУРИРОВАННЫЙ JSON-ЭКСПОРТ:")
    print(latest_path.name)

    print(
        "\nВопрос, период и полнота истории уже записаны внутри JSON."
    )
    print(
        "Файл сохранён локально; программа сама не передаёт его внешним сервисам."
    )

    print("\nДатированная копия:")
    print(str(archive_path))

    if settings.get(
        "open_output_folder",
        True,
    ):
        open_and_select_file(
            latest_path
        )

    log_info(
        "SEARCH DONE | "
        f"question={question!r} | "
        f"days={overview['period_days']} | "
        f"hits={overview['total_direct_hits']} | "
        f"complete={history_status['complete']} | "
        f"method={engine['method']}"
    )

    return latest_path


def open_and_select_file(path):
    if os.name != "nt":
        return

    try:
        subprocess.Popen(
            [
                "explorer.exe",
                f"/select,{str(path)}",
            ]
        )
    except Exception as e:
        log_error(
            f"Не удалось открыть Проводник: {e}"
        )


# ============================================================
# Main
# ============================================================

async def ensure_telegram_client(
    client,
    creds,
    settings=None,
    allow_reauthorization=False,
):
    """
    Подключается к Telegram только когда это действительно нужно.
    Поиск по локальной news.db может работать вообще без Telegram.

    Если credentials.bin уже существует, но сохранённая сессия исчезла
    или перестала быть авторизованной, новый вход автоматически не
    запускается. Повторная авторизация разрешается только после явной
    команды пользователя через восстановление Telegram-сессии.
    """
    if client is not None:
        try:
            if client.is_connected():
                return client, creds
        except Exception:
            pass

    stored_credentials_at_start = CRED_FILE.exists()
    session_path = Path(SESSION_FILE + ".session")

    if (
        stored_credentials_at_start
        and not session_path.exists()
        and not allow_reauthorization
    ):
        raise RuntimeError(
            "Найдены сохранённые Telegram credentials, но файл "
            "telegram_session.session отсутствует. Автоматическая "
            "повторная авторизация отключена. Восстановите сессию из "
            "резервной копии или выберите T в главном меню, чтобы явно "
            "создать новую Telegram-сессию."
        )

    # В Testing-ветке любой FloodWait должен дойти до нашего обработчика.
    flood_sleep_threshold = 0

    while True:
        if creds is None:
            creds = load_or_create_credentials()

        candidate = TelegramClient(
            SESSION_FILE,
            creds["api_id"],
            creds["api_hash"],
            flood_sleep_threshold=flood_sleep_threshold,
        )

        print("\nПодключение к Telegram...")

        try:
            await candidate.connect()
            authorized = await candidate.is_user_authorized()

            if (
                stored_credentials_at_start
                and not authorized
                and not allow_reauthorization
            ):
                try:
                    await candidate.disconnect()
                finally:
                    raise RuntimeError(
                        "Сохранённая Telegram-сессия больше не "
                        "авторизована. Автоматический повторный вход "
                        "отключён, чтобы не создавать лишние попытки "
                        "авторизации. Проверьте состояние аккаунта; "
                        "если хотите создать новую сессию осознанно, "
                        "выберите T в главном меню."
                    )

            if not authorized:
                await candidate.start(
                    phone=creds["phone"],
                    code_callback=prompt_telegram_code,
                    password=prompt_telegram_password,
                )

        except ApiIdInvalidError:
            try:
                await candidate.disconnect()
            except Exception:
                pass

            if stored_credentials_at_start:
                raise RuntimeError(
                    "Telegram отклонил ранее сохранённые API ID / API Hash. "
                    "Автоматическая замена credentials и повторная "
                    "авторизация отключены."
                )

            print(
                "\nTelegram отклонил API ID / API Hash. "
                "Проверьте значения на "
                "https://my.telegram.org → API development tools."
            )
            print(
                "Введите API ID и API Hash заново."
            )

            creds = None
            client = None
            continue

        except Exception:
            try:
                await candidate.disconnect()
            except Exception:
                pass
            raise

        save_credentials(creds)
        print(
            "Telegram успешно подключён. "
            "Сессия сохранена — повторный ввод обычно не потребуется."
        )
        return candidate, creds

async def recreate_telegram_session(client, creds, settings=None):
    """Явно пересоздаёт только локальную Telegram-сессию по команде пользователя."""
    print("\n=== Восстановление Telegram-сессии ===")
    print(
        "Используйте это, если telegram_session.session отсутствует, "
        "отозван или вы сами хотите войти заново."
    )
    print(
        "Будет удалён только локальный файл Telegram-сессии. "
        "credentials.bin, выбранные каналы, news.db и дайджесты сохранятся."
    )
    choice = input("1 — создать новую сессию; 0 — назад: ").strip()
    if choice != "1":
        print("Восстановление отменено.")
        return client, creds

    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
        client = None

    for suffix in (".session", ".session-journal"):
        path = Path(SESSION_FILE + suffix)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось удалить локальный файл сессии {path.name}: {exc}"
            ) from exc

    print(
        "Локальная Telegram-сессия очищена. "
        "Сейчас Telegram запросит новый код входа."
    )
    return await ensure_telegram_client(
        None,
        creds,
        settings,
        allow_reauthorization=True,
    )



async def _v4_main():
    if os.name != "nt":
        print(
            "Эта версия предназначена для Windows."
        )
        return

    settings = load_settings()
    cleanup_old_files(settings)

    conn = open_db()

    print("\nОбслуживание локальной базы...")
    db_maintenance = maintain_database(
        conn,
        settings,
    )

    if db_maintenance["deleted_by_age"]:
        print(
            f"  Удалено сообщений старше "
            f"{db_maintenance['retention_days']} дней: "
            f"{db_maintenance['deleted_by_age']}"
        )

    if db_maintenance["triggered"]:
        print(
            f"  Сработал лимит news.db: "
            f"{db_maintenance['size_before_mb']:.2f} → "
            f"{db_maintenance['size_after_mb']:.2f} МБ "
            f"(лимит {db_maintenance['limit_mb']} МБ)"
        )
        print(
            f"  Дополнительно удалено самых старых сообщений: "
            f"{db_maintenance['messages_deleted']}"
        )
    else:
        print(
            f"  news.db: "
            f"{db_maintenance['size_after_mb']:.2f} МБ "
            f"из {db_maintenance['limit_mb']} МБ"
        )

    if db_maintenance["quick_check_ran"]:
        if db_maintenance["quick_check_ok"]:
            print("  SQLite quick_check: OK")
        else:
            print(
                "  ВНИМАНИЕ: SQLite quick_check: "
                f"{db_maintenance['quick_check_result']}"
            )
    else:
        print(
            "  SQLite quick_check: "
            f"{db_maintenance['quick_check_result']} "
            "(проверка ещё не требуется)"
        )

    client = None
    creds = None

    try:
        channels = None
        hours = None

        while True:
            print(
                "\nРежим работы:"
            )
            print(
                "  Enter / D — обычный дайджест"
            )
            print(
                "  S         — быстрый поиск по локальной базе"
            )
            print(
                "  B         — заранее заполнить базу историей за 90 дней"
            )
            print(
                "  A         — добавить канал(ы)"
            )
            print(
                "  R         — удалить канал"
            )
            print(
                "  L         — показать список каналов"
            )
            print(
                "  N         — создать список каналов заново"
            )
            print("  I         — состояние базы и каналов")
            print("  T         — восстановить Telegram-сессию")
            print(
                "  0         — выйти"
            )

            mode = input(
                "Выбор [Enter/D/S/B/A/R/L/N/I/T/0]: "
            ).strip().lower()

            if mode in ("i", "status"):
                print_database_status(conn, load_selection())
                continue

            if mode in ("t", "session", "сессия", "восстановить"):
                client, creds = await recreate_telegram_session(
                    client,
                    creds,
                    settings,
                )
                continue

            if is_back_command(mode) or mode in (
                "q", "quit", "exit", "в", "выход"
            ):
                print("\nВыход.")
                return

            if mode in ("s", "search", "п", "поиск"):
                channels = dedupe_channel_items(load_selection())

                if channels and print_channel_limit_error(channels):
                    continue

                if not channels:
                    print(
                        "\nНет выбранных каналов."
                    )
                    continue

                await run_history_search_mode(
                    client,
                    conn,
                    channels,
                    settings,
                    db_maintenance,
                )
                continue

            if mode in ("b", "backfill", "б", "история"):
                client, creds = await ensure_telegram_client(
                    client,
                    creds,
                    settings,
                )

                channels = await resolve_channels(
                    client,
                    skip_menu=True,
                )

                if not channels:
                    print(
                        "\nНет выбранных каналов."
                    )
                    continue

                await prefill_full_history(
                    client,
                    conn,
                    channels,
                    settings,
                )
                continue

            channel_action_aliases = {
                "a": "a",
                "add": "a",
                "д": "a",
                "добавить": "a",
                "r": "r",
                "remove": "r",
                "у": "r",
                "удалить": "r",
                "l": "l",
                "list": "l",
                "с": "l",
                "список": "l",
                "n": "n",
                "new": "n",
                "н": "n",
                "заново": "n",
            }

            if mode in channel_action_aliases:
                channel_action = channel_action_aliases[mode]

                # Только добавление публичного канала и пересоздание списка
                # требуют Telegram. Просмотр и удаление работают локально.
                if channel_action in ("a", "n"):
                    client, creds = await ensure_telegram_client(
                        client,
                        creds,
                        settings,
                    )

                await resolve_channels(
                    client,
                    initial_action=channel_action,
                    return_after_initial=True,
                    skip_menu=False,
                )
                print(
                    "\nВозврат в главное меню. Текущий список сохранён."
                )
                print(
                    "Можно сразу запустить дайджест, поиск или выполнить другую команду."
                )
                continue

            if mode in ("", "d", "digest", "д", "дайджест"):
                client, creds = await ensure_telegram_client(
                    client,
                    creds,
                    settings,
                )

                channels = await resolve_channels(
                    client,
                    skip_menu=True,
                )

                if not channels:
                    print(
                        "\nНет выбранных каналов."
                    )
                    continue

                default_hours = settings.get(
                    "default_hours",
                    24,
                )

                print("0 — Назад в главное меню.")
                raw_hours = input(
                    "\nЗа сколько последних часов "
                    f"сделать выгрузку? "
                    f"[{default_hours}]: "
                ).strip()

                if is_back_command(raw_hours):
                    print("Создание дайджеста отменено.")
                    channels = None
                    continue

                try:
                    hours = (
                        float(raw_hours)
                        if raw_hours
                        else float(default_hours)
                    )
                except ValueError:
                    hours = float(default_hours)

                if hours <= 0:
                    hours = float(default_hours)

                break

            print("\nНеизвестная команда. Выберите пункт из меню.")

        (
            previous_run_utc,
            previous_digest_source,
        ) = get_previous_digest_reference(
            conn,
            channels,
        )

        if previous_run_utc:
            print(
                "\nТочка сравнения для этого набора каналов найдена:"
            )
            print(
                f"  {previous_run_utc}"
            )
            print(
                f"  источник: "
                f"{previous_digest_source}"
            )
        else:
            print(
                "\nДля этого набора каналов предыдущий дайджест не найден — "
                "этот запуск станет базовой точкой."
            )

        history_status = await ensure_history_for_search(
            client, conn, channels, hours / 24, settings)
        # Move statistics out before attaching coverage to them. Retaining
        # both directions would create a cycle that JSON cannot serialize.
        sync_stats = history_status.pop('sync_stats')
        if not history_status['complete']:
            print('ВНИМАНИЕ: период загружен не полностью. Подробности сохранены в выгрузке.')
        sync_stats["history_completeness"] = history_status

        sync_stats["self_diagnostics"] = {
            "collector_version": APP_VERSION,
            "database_integrity": {
                "quick_check_ok": (
                    db_maintenance[
                        "quick_check_ok"
                    ]
                ),
                "quick_check_result": (
                    db_maintenance[
                        "quick_check_result"
                    ]
                ),
                "quick_check_utc": (
                    db_maintenance[
                        "quick_check_utc"
                    ]
                ),
                "quick_check_ran_this_start": (
                    db_maintenance[
                        "quick_check_ran"
                    ]
                ),
            },
            "comparison": {
                "available": bool(
                    previous_run_utc
                ),
                "baseline_utc": (
                    previous_run_utc
                ),
                "source": (
                    previous_digest_source
                ),
                "scope": "same_channel_selection",
                "selection_fingerprint": (
                    selection_fingerprint(channels)
                ),
            },
        }

        raw_messages = (
            load_messages_for_export(
                conn,
                channels,
                hours,
                previous_run_utc,
                settings,
            )
        )

        period_messages = [
            message
            for message in raw_messages
            if message.get("in_selected_period", True)
        ]

        news, operational = (
            split_operational(
                period_messages,
                settings,
            )
        )

        news, exact_count, near_count = (
            collapse_duplicates(
                news,
                settings,
            )
        )

        related_groups = build_related_groups(
            news
        )

        (
            latest_path,
            archive_path,
            raw_path,
            change_summary,
        ) = save_output(
            raw_messages,
            news,
            operational,
            exact_count,
            near_count,
            hours,
            channels,
            sync_stats,
            previous_run_utc,
            previous_digest_source,
            related_groups,
            settings,
        )

        register_run(
            conn,
            hours,
            len(raw_messages),
            sync_stats[
                "successful_channels"
            ],
            sync_stats[
                "failed_channels"
            ],
            channels,
        )

        db_size_mb = (
            database_file_size_mb()
        )

        print("\n" + "=" * 58)
        print("ГОТОВО")
        print("=" * 58)

        print(
            f"Каналов успешно: "
            f"{sync_stats['successful_channels']}"
            f"/{len(channels)}"
        )

        if not history_status.get("complete"):
            print("\nВНИМАНИЕ: история за выбранный период неполная. Подробности в просмотре.")
        if sync_stats[
            "failed_channels"
        ]:
            print(
                f"Каналов с ошибкой: "
                f"{sync_stats['failed_channels']}"
            )

        print(
            f"Новых сообщений добавлено в базу: "
            f"{sync_stats['new_messages_saved']}"
        )
        print(
            f"Содержательно изменённых постов: "
            f"{sync_stats['content_changed_messages_refreshed']}"
        )
        print(
            f"Постов с обновившимися метриками: "
            f"{sync_stats['metrics_changed_messages_refreshed']}"
        )

        if sync_stats["migrated_messages"]:
            print(
                f"Строк базы переведено на схему 4.0: "
                f"{sync_stats['migrated_messages']}"
            )

        print(
            f"\nСообщений за выбранный период: "
            f"{len(raw_messages)}"
        )
        print(
            f"Новостей после очистки: "
            f"{len(news)}"
        )
        print(
            f"Рутинных сообщений отдельно: "
            f"{len(operational)}"
        )
        print(
            f"Удалено точных дублей: "
            f"{exact_count}"
        )
        print(
            f"Удалено почти точных дублей: "
            f"{near_count}"
        )
        print(
            f"Групп связанных сообщений: "
            f"{len(related_groups)}"
        )

        if previous_run_utc:
            print("\nС прошлого дайджеста:")
            print(
                "  новых сообщений: "
                f"{change_summary.get('new_since_previous_digest', 0)}"
            )
            print(
                "  содержательно изменённых: "
                f"{change_summary.get('edited_since_previous_digest', 0)}"
            )
            print(
                "  только метрики изменились: "
                f"{change_summary.get('metrics_changed_since_previous_digest', 0)}"
            )
            print(
                "  точка сравнения: "
                f"{previous_digest_source}"
            )
            print(
                "  В JSON уже есть готовый блок "
                "changes_since_previous_digest — "
                "сравнивать файлы вручную не нужно."
            )
        else:
            print(
                "\nЭто базовый дайджест для текущего набора каналов. "
                "Следующий запуск с тем же набором уже даст точное сравнение."
            )

        print(
            f"\nРазмер news.db: "
            f"{db_size_mb:.2f} МБ "
            f"(лимит {int(settings.get('max_database_mb', 500))} МБ)"
        )
        print(
            f"История в базе: до "
            f"{int(settings.get('database_retention_days', 90))} дней"
        )
        print(
            f"Архив дайджестов: "
            f"{int(settings.get('archive_retention_days', 30))} дней; "
            f"сырые копии: "
            f"{int(settings.get('raw_retention_days', 7))} дней; "
            f"логи: "
            f"{int(settings.get('log_retention_days', 14))} дней"
        )

        if AI_LATEST_FILE.exists():
            print(
                "\nФАЙЛ ДЛЯ ИИ:"
            )
            print(AI_LATEST_FILE.name)
            print(
                "\nПуть:"
            )
            print(str(AI_LATEST_FILE))

        print(
            "\nПОЛНЫЙ ТЕХНИЧЕСКИЙ JSON:"
        )
        print(
            latest_path.name
        )

        print(
            "\nПуть:"
        )
        print(str(latest_path))

        print(
            "\nДатированная копия сохранена:"
        )
        print(str(archive_path))

        if raw_path:
            print(
                "\nСырая резервная копия:"
            )
            print(str(raw_path))

        if sync_stats[
            "failed_channels"
        ]:
            print(
                "\nВНИМАНИЕ: часть каналов "
                "не синхронизировалась."
            )
            print(
                "Ошибки записаны в папку logs."
            )
        else:
            print(
                "\nКонтроль качества: "
                "все выбранные каналы синхронизированы."
            )

        print(
            "\nФайл сохранён локально. Программа сама не передаёт "
            "Telegram-контент внешним сервисам."
        )

        if settings.get(
            "open_output_folder",
            True,
        ):
            open_and_select_file(
                AI_LATEST_FILE
                if AI_LATEST_FILE.exists()
                else latest_path
            )

        log_info(
            "DONE | "
            f"channels={len(channels)} | "
            f"ok={sync_stats['successful_channels']} | "
            f"failed={sync_stats['failed_channels']} | "
            f"period_messages={len(raw_messages)} | "
            f"news={len(news)} | "
            f"db_mb={db_size_mb:.2f}"
        )

    finally:
        try:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        finally:
            if settings.get(
                "sqlite_optimize_on_close",
                True,
            ):
                optimize_result = optimize_database(
                    conn
                )
                if not optimize_result[
                    "optimize_ok"
                ]:
                    log_error(
                        "PRAGMA optimize failed: "
                        f"{optimize_result['optimize_error']}"
                    )
            conn.close()


# ============================================================
# Version 5: safe migration, history, retrieval and local viewer
# ============================================================

\
class InstanceLock:
    """Single-instance guard released automatically even after a crash."""

    GLOBAL_MUTEX_NAME = r"Local\TelegramNewsAI.SingleInstance"
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, path=None):
        self.explicit_path = path is not None
        self.path = Path(path or APP_DIR / 'collector.lock')
        self.file = None
        self.mutex_handle = None

    def __enter__(self):
        # Обычный Windows-запуск использует именованный kernel mutex.
        # Он блокирует вторую копию даже из другой папки установки.
        if os.name == 'nt' and not self.explicit_path:
            kernel32 = ctypes.windll.kernel32
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            create_mutex.restype = wintypes.HANDLE

            handle = create_mutex(None, False, self.GLOBAL_MUTEX_NAME)
            if not handle:
                raise ctypes.WinError()

            error = kernel32.GetLastError()
            if error == self.ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                raise RuntimeError(
                    'Unofficial TelegramNewsAI уже запущен в другом окне или из другой папки. '
                    'Закройте предыдущий экземпляр и повторите запуск.'
                )

            self.mutex_handle = handle
            return self

        # Явный path сохраняет прежнюю файловую блокировку для
        # тестов/служебных сценариев и для не-Windows платформ.
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)

        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError(
                'Программа уже открыта. Завершите предыдущий сбор и повторите запуск.'
            )
        return self

    def __exit__(self, *args):
        if self.mutex_handle:
            ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None
        if self.file:
            self.file.close()
            self.file = None


def open_db():
    # SQLite backup includes committed WAL pages. Never copy the live DB as bytes.
    if DB_FILE.exists():
        check = sqlite3.connect(DB_FILE)
        try:
            cols = {r[1] for r in check.execute('PRAGMA table_info(messages)')}
            if 'raw_text' not in cols:
                folder = APP_DIR / 'Резервные_копии'
                folder.mkdir(exist_ok=True)
                dest = folder / ('news_before_v5_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.db')
                backup = sqlite3.connect(dest)
                try:
                    check.backup(backup)
                    if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Не прошла проверка резервной копии базы. Обновление остановлено.')
                finally:
                    backup.close()
        finally:
            check.close()
    conn = _v4_open_db()
    try:
        conn.execute('PRAGMA busy_timeout=10000')
        conn.create_function('unicode_fold', 1, lambda x: search_fold(x or ''), deterministic=True)
        for name, typ in [('raw_text','TEXT'), ('availability','TEXT DEFAULT \'available\''),
                          ('availability_checked_utc','TEXT'), ('unavailable_since_utc','TEXT')]:
            ensure_column(conn, 'messages', name, typ)
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS message_versions (
                id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                captured_utc TEXT NOT NULL, snapshot_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS versions_message ON message_versions(channel_id,message_id,id);
            CREATE TABLE IF NOT EXISTS semantic_vectors (
                channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, model TEXT NOT NULL,
                content_hash TEXT NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY(channel_id,message_id,model)
            );
            CREATE TRIGGER IF NOT EXISTS messages_v5_cleanup AFTER DELETE ON messages BEGIN
                DELETE FROM message_versions WHERE channel_id=old.channel_id AND message_id=old.message_id;
                DELETE FROM semantic_vectors WHERE channel_id=old.channel_id AND message_id=old.message_id;
                UPDATE channels SET history_coverage_utc=NULL WHERE channel_id=old.channel_id;
            END;
        ''')
        # Remove at most obsolete versions from the embedding cache, not message history.
        conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('schema_version','5')")
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def stored_semantic_message(row):
    data = dict(row)
    external_urls = json_loads(data.get("external_urls_json"), [])
    canonical_urls = json_loads(data.get("canonical_urls_json"), [])
    if not canonical_urls:
        canonical_urls = normalize_external_urls(external_urls)

    return {
        "text": data.get("text"),
        "media": json_loads(data.get("media_json"), {}),
        "album_id": data.get("album_id"),
        "reply_to_message_id": data.get("reply_to_message_id"),
        "post_author": data.get("post_author"),
        "forwarded_from": enrich_forward_info_links(
            json_loads(data.get("forwarded_from_json"), None)
        ),
        "canonical_urls": canonical_urls,
    }


def upsert_message(conn, message):
    key = (int(message['channel_id']), int(message['message_id']))
    old = conn.execute('SELECT * FROM messages WHERE channel_id=? AND message_id=?',key).fetchone()
    semantic_changed = False
    raw_changed = False
    if old is not None:
        semantic_changed = (
            semantic_content_hash(stored_semantic_message(old))
            != semantic_content_hash(message)
        )
        raw_changed = (
            old['raw_text'] is not None
            and old['raw_text'] != message.get('raw_text')
        )
    if old is not None and (semantic_changed or raw_changed):
        snapshot = dict(old)
        conn.execute('INSERT INTO message_versions(channel_id,message_id,captured_utc,snapshot_json) VALUES(?,?,?,?)',
                     (*key,iso_utc(utc_now()),json_dumps(snapshot)))
    status = _v4_upsert_message(conn,message)
    conn.execute('''UPDATE messages SET raw_text=?, availability='available',
                    availability_checked_utc=?, unavailable_since_utc=NULL
                    WHERE channel_id=? AND message_id=?''',
                 (message.get('raw_text'),iso_utc(utc_now()),*key))
    return status


def db_row_to_message(row, previous_run_utc):
    result = _v4_db_row_to_message(row, previous_run_utc)
    data = dict(row)
    result.update(raw_text=data.get('raw_text'),raw_text_available=data.get('raw_text') is not None,
                  availability=data.get('availability') or 'available',
                  availability_checked_utc=data.get('availability_checked_utc'),
                  unavailable_since_utc=data.get('unavailable_since_utc'),
                  channel_url=public_channel_link(result.get('username')),
                  telegram_url=result.get('telegram_url') or public_link(
                      result.get('username'), result.get('message_id')))
    if data.get('unavailable_since_utc') and (not previous_run_utc or data['unavailable_since_utc'] > previous_run_utc):
        result['change_status'] = 'unavailable_since_previous_digest'
    return result


def normalize_previous_version(snapshot, captured_utc):
    """Оставляет в истории редакций только поля, помогающие понять изменение."""
    result = {
        "captured_utc": captured_utc,
        "text": snapshot.get("text"),
        "raw_text": snapshot.get("raw_text"),
        "raw_text_available": snapshot.get("raw_text") is not None,
    }

    for field in (
        "edit_date_utc",
        "edit_date_local",
        "album_id",
        "reply_to_message_id",
        "post_author",
    ):
        value = snapshot.get(field)
        if value is not None:
            result[field] = value

    media = json_loads(snapshot.get("media_json"), {})
    if media:
        result["media"] = media

    forwarded_from = enrich_forward_info_links(
        json_loads(snapshot.get("forwarded_from_json"), None)
    )
    if forwarded_from:
        result["forwarded_from"] = forwarded_from

    external_urls = json_loads(snapshot.get("external_urls_json"), [])
    if external_urls:
        result["external_urls"] = external_urls

    canonical_urls = json_loads(snapshot.get("canonical_urls_json"), [])
    if not canonical_urls and external_urls:
        canonical_urls = normalize_external_urls(external_urls)
    if canonical_urls:
        result["canonical_urls"] = canonical_urls

    return result


def attach_versions(conn, messages, limit=10):
    limit = max(0, int(limit))
    for start in range(0, len(messages), 250):
        batch = messages[start:start+250]
        by_key = {(item['channel_id'], item['message_id']): item for item in batch}
        for item in batch:
            item.update(versions_count=0, versions_truncated=False, previous_versions=[])
        values = ','.join('(?,?)' for _ in by_key)
        params = [value for key in by_key for value in key]
        rows = conn.execute('''WITH picked(channel_id,message_id) AS (VALUES '''+values+'''), ranked AS (
            SELECT v.*, COUNT(*) OVER(PARTITION BY v.channel_id,v.message_id) AS total,
            ROW_NUMBER() OVER(PARTITION BY v.channel_id,v.message_id ORDER BY v.id DESC) AS position
            FROM message_versions v JOIN picked p USING(channel_id,message_id))
            SELECT * FROM ranked WHERE position<=? ORDER BY id ASC''', (*params, max(1,limit))).fetchall()
        for row in rows:
            item = by_key[(row['channel_id'], row['message_id'])]
            item['versions_count'] = row['total']
            item['versions_truncated'] = row['total'] > limit
            if limit:
                item['previous_versions'].append(
                    normalize_previous_version(
                        json_loads(row['snapshot_json'], {}),
                        row['captured_utc'],
                    )
                )
    return messages


async def reconcile_missing_messages(client, conn, channel, settings, cutoff):
    limit = max(0,int(settings.get('deletion_check_limit',300)))
    if not limit:
        return {'checked':0,'complete':False,'reason':'disabled'}
    rows = conn.execute('''SELECT message_id FROM messages WHERE channel_id=? AND date_utc>=?
        ORDER BY availability_checked_utc ASC LIMIT ?''',(channel['id'],iso_utc(cutoff),limit)).fetchall()
    checked = missing = 0
    try:
        for start in range(0,len(rows),100):
            ids = [r['message_id'] for r in rows[start:start+100]]
            found = await client.get_messages(channel['entity'], ids=ids)
            # Treat only an explicit None at the corresponding ID as unavailable.
            # Permission/network failures do not imply deletion.
            if len(found) != len(ids):
                raise RuntimeError('Неполный ответ проверки доступности сообщений')
            for mid, item in zip(ids,found):
                now = iso_utc(utc_now())
                if item is None:
                    conn.execute('''UPDATE messages SET availability='unavailable',availability_checked_utc=?,
                        unavailable_since_utc=coalesce(unavailable_since_utc,?) WHERE channel_id=? AND message_id=?''',
                        (now,now,channel['id'],mid))
                    missing += 1
                else:
                    conn.execute('UPDATE messages SET availability_checked_utc=? WHERE channel_id=? AND message_id=?',
                                 (now,channel['id'],mid))
                checked += 1
        return {'checked':checked,'unavailable':missing,'scope':'recent_window','limit':limit}
    except Exception as e:
        log_error('Availability check: '+str(e))
        return {'checked':checked,'unavailable':missing,'complete':False,'error':str(e)}


async def ensure_history_for_search(client, conn, channels, days, settings, skip_fresh=False):
    # This also accepts fractional days for a one-hour digest.
    requested = max(1/24,float(days))
    effective = min(requested,max(1,int(settings.get('database_retention_days',90))))
    cutoff = utc_now()-timedelta(days=effective)
    stale = set(local_history_status(conn, channels, days, settings)['stale_channel_ids'])
    pending = [ch for ch in channels if not skip_fresh or ch['id'] in stale]
    stats = await sync_all_channels(client,conn,pending,effective*24,settings)
    stats['successful_channels'] += len(channels) - len(pending)
    coverage = []
    for ch in channels:
        print('  Проверка периода: '+ch['name'])
        coverage.append(await backfill_channel_history_with_retries(client,conn,ch,cutoff,settings))
    complete_count = sum(bool(x.get('complete')) for x in coverage)
    return dict(requested_days=requested,effective_days=effective,cutoff_utc=iso_utc(cutoff),
        selected_channels=len(channels),complete_channels=complete_count,
        incomplete_channels=len(channels)-complete_count,
        complete=complete_count==len(channels) and stats['failed_channels']==0 and requested==effective,
        period_limited=requested>effective,channel_coverage=coverage,
        warnings=[x for x in coverage if not x.get('complete')],
        sync_quality={k:stats[k] for k in ('successful_channels','failed_channels','new_messages_saved')},
        sync_stats=stats)


def load_messages_for_export(conn, channels, hours, previous_run_utc, settings=None):
    ids=[int(c['id']) for c in channels]
    if not ids:
        return []
    cutoff_dt=utc_now()-timedelta(hours=hours)
    cutoff=iso_utc(cutoff_dt)
    baseline=previous_run_utc or '9999'
    rows=conn.execute('''SELECT * FROM messages WHERE channel_id IN ('''+','.join('?' for _ in ids)+''')
        AND (date_utc>=? OR content_changed_utc>? OR unavailable_since_utc>?) ORDER BY date_utc''',
        (*ids,cutoff,baseline,baseline)).fetchall()
    messages=attach_versions(conn,[db_row_to_message(r,previous_run_utc) for r in rows],
        max(0, int((settings or DEFAULT_SETTINGS).get('revision_export_limit', 3))))
    for message in messages:
        message_date=parse_dt(message.get('date_utc'))
        message['in_selected_period']=bool(message_date and message_date>=cutoff_dt)
    return messages


def collapse_duplicates(messages, settings):
    # Near duplicates are linked but never suppressed: even one changed letter
    # can change a name, a number or a negation. Exact copies retain full snapshots.
    kept=[]; exact=0; buckets={}
    window=max(0,float(settings.get('near_duplicate_window_hours',3)))*3600
    for source in messages:
        m=copy.deepcopy(source)
        m.setdefault('duplicates',[])
        text=unicodedata.normalize('NFC',m.get('text') or '').strip()
        media = m.get('media') or {}
        media_identity = media.get('identity') if isinstance(media, dict) else None
        # Старые строки БД не содержат identity. Для них безопаснее НЕ
        # схлопнуть потенциально разные медиа, чем потерять публикацию.
        # Текстовые сообщения по-прежнему дедуплицируются между каналами.
        media_guard = None
        if isinstance(media, dict) and media.get('type') and not media_identity:
            media_guard = message_key(m)
        fingerprint=json_dumps([text,media,m.get('canonical_urls'),m.get('raw_text'),
                               media_guard])
        target=None
        for candidate in reversed(buckets.get(fingerprint,[])):
            a,b=parse_dt(m.get('date_utc')),parse_dt(candidate.get('date_utc'))
            if a and b and abs((a-b).total_seconds())<=window:
                target=candidate; break
        if settings.get('remove_exact_duplicates',True) and len(text)>=40 and target is not None:
            snapshot=copy.deepcopy(m); snapshot.pop('duplicates',None)
            inherited = []
            for field in ('text', 'raw_text'):
                if field in snapshot and snapshot[field] == target.get(field):
                    snapshot.pop(field); inherited.append(field)
            snapshot['inherits_from_message_key'] = message_key(target)
            snapshot['inherited_fields'] = inherited
            target['duplicates'].append(snapshot); exact+=1
            continue
        if settings.get('remove_near_duplicates',True) and len(text)>=120:
            for c in reversed(kept[-120:]):
                a,b=parse_dt(m.get('date_utc')),parse_dt(c.get('date_utc'))
                if not a or not b or abs((a-b).total_seconds())>window:
                    continue
                ct=c.get('text') or ''
                if abs(len(text)-len(ct)) > max(len(text),len(ct))*0.1:
                    continue
                threshold = float(settings.get('near_duplicate_threshold',0.985))
                matcher = SequenceMatcher(None,text,ct,autojunk=False)
                # Both cheap scores are upper bounds: rejecting below the
                # threshold cannot discard a qualifying similarity match.
                if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                    continue
                if matcher.ratio()>=threshold:
                    m.setdefault('similar_message_refs',[]).append(message_key(c))
                    m['version_note']='Похожая публикация: различия сохранены, это не точный дубль.'
                    break
        kept.append(m); buckets.setdefault(fingerprint,[]).append(m)
    return kept,exact,0


def build_changes_block(news_messages, operational_messages, previous_digest_utc):
    # Include individual reposts that may have been collapsed in the full view.
    expanded_news = [entry for message in news_messages for entry in [message, *message.get('duplicates',[])]]
    result=_v4_build_changes_block(expanded_news,operational_messages,previous_digest_utc)
    result['unavailable_message_refs']=[make_compact_message_ref(m) for m in news_messages+operational_messages
        if m.get('change_status')=='unavailable_since_previous_digest']
    return result


def maintain_database(conn, settings):
    # Versions and vectors share the parent post's retention via a delete trigger.
    return _v4_maintain_database(conn,settings)


def search_fold(text):
    return unicodedata.normalize('NFC',text).casefold().replace('ё','е').replace('’',"'").replace('ʼ',"'")


class SearchPhrase(str):
    """Preserves an explicit quote even for a single word or abbreviation."""


def extract_search_terms(question):
    phrases=re.findall(r'"([^"\n]+)"|«([^»\n]+)»',question or '')
    remainder=re.sub(r'"[^"\n]+"|«[^»\n]+»',' ',question or '')
    terms=[SearchPhrase(' '.join(search_fold(a or b).split())) for a,b in phrases if (a or b).strip()]
    for token in re.findall(r"[^\W_]+(?:['’-][^\W_]+)*",remainder,flags=re.UNICODE):
        folded=search_fold(token)
        if len(folded)>=2 and folded not in SEARCH_STOPWORDS:
            terms.append(folded)
    return list(dict.fromkeys(terms))


def search_term_prefix(token, settings):
    # Never cut every word to five letters. This helper remains for compatibility.
    return token


SEARCH_ALIASES = [
    ('ес','єс','евросоюз','євросоюз','европейский союз','європейський союз'),
    ('сша','соединенные штаты','сполучені штати','usa'),
    ('харьков','харькове','харькова','харькову','харьковом','харків','харкові','харкова','харкову','харковом'),
    ('киев','киеве','киева','киеву','киевом','київ','києві','києва','києву','києвом'),
    ('прокуратура','прокуратури','прокуратуру','прокуратурой'),
    ('мобилизация','мобилизации','мобилизацию','мобілізація','мобілізації','мобілізацію'),
    ('тцк','территориальный центр комплектования','територіальний центр комплектування'),
    ('генеральная','генеральной','генеральну','генеральна','генеральної'),
    ('увольнения','увольнение','сокращение персонала','звільнення','скорочення персоналу'),
    ('завод','завода','заводе','заводу','предприятие','предприятия','предприятии','підприємство','підприємства','підприємстві'),
    ('закрытие','закрытия','закрытии','закриття','закритті'),

    # Частый украинский именованный объект. Благодаря двум отдельным
    # группам запрос «Новая почта» находит «Нова пошта» и Nova Poshta,
    # но только когда совпали ОБА смысловых компонента.
    ('новая','новой','новую','нова','нової','новій','нову','новою','nova'),
    ('почта','почты','почте','почту','почтой','почтою','пошта','пошти','пошті','пошту','поштою','poshta'),
]


SEARCH_PHRASE_ALIASES = [
    ('новая почта','нова пошта','nova poshta'),
    ('укрпочта','укрпошта','ukrposhta'),
    ('верховная рада','верховна рада'),
    ('воздушная тревога','повітряна тривога'),
    ('национальный банк','національний банк'),
]


# Осторожный морфологический префикс: не «режем всё до пяти букв», а
# убираем только типичные окончания, сохраняя минимум четыре буквы корня.
# Это позволяет находить словоформы одного поискового термина.
_SEARCH_INFLECTION_SUFFIXES = tuple(sorted({
    'иями','ями','ами','иями','овими','евими','ими','ыми',
    'ого','ему','ому','ої','ою','ею','ами','ями','ах','ях','ам','ям',
    'ов','ев','ей','ий','ый','ій','ая','яя','ое','ее','ие','ые','ую','юю',
    'ою','ею','ом','ем','ой','ою','ам','ям','ах','ях',
    'а','я','ы','и','у','ю','е','о','і','ї','й',
}, key=len, reverse=True))


def search_stem_prefix(token):
    token = search_fold(str(token)).strip()
    if ' ' in token or len(token) < 5 or not token.replace("'", '').isalpha():
        return token
    for suffix in _SEARCH_INFLECTION_SUFFIXES:
        if token.endswith(suffix):
            root = token[:-len(suffix)]
            # Корни короче пяти букв слишком часто дают омонимы: например
            # почт* совпадает не только с «почта», но и с «почти».
            if len(root) >= 5:
                return root
    return token


def term_variants(term, settings):
    # Явно процитированная пользователем фраза остаётся точной.
    if isinstance(term, SearchPhrase):
        return [str(term)]
    groups = SEARCH_ALIASES + settings.get('search_alias_groups', [])
    normalized_term = search_fold(str(term))
    for group in groups:
        normalized = [search_fold(str(x)) for x in group]
        if normalized_term in normalized:
            return unique_keep_order([normalized_term] + normalized)
    return [normalized_term]


def query_phrase_variants(question, terms, settings):
    # Если пользователь сам взял фразу в кавычки — не расширяем её синонимами.
    explicit = [str(t) for t in terms if isinstance(t, SearchPhrase)]
    if explicit:
        return unique_keep_order(explicit)

    plain = [str(t) for t in terms]
    if not (2 <= len(plain) <= 6):
        return []

    phrase = ' '.join(plain)
    groups = SEARCH_PHRASE_ALIASES + settings.get('search_phrase_alias_groups', [])
    for group in groups:
        normalized = [search_fold(str(x)) for x in group]
        if phrase in normalized:
            return unique_keep_order([phrase] + normalized)
    return [phrase]


def _fts_variant_expression(value):
    value = search_fold(str(value))
    escaped = value.replace('"', '""')
    if ' ' in value:
        return 'text:"' + escaped + '"'
    stem = search_stem_prefix(value)
    if stem != value:
        return '(text:"' + escaped + '" OR text:"' + stem.replace('"','""') + '"*)'
    return 'text:"' + escaped + '"'


def build_fts_query(terms, settings, operator='AND'):
    groups = []
    for term in terms:
        variants = term_variants(term, settings)
        groups.append('(' + ' OR '.join(_fts_variant_expression(v) for v in variants) + ')')
    return (' ' + operator + ' ').join(groups)


def _text_tokens(text):
    return re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", search_fold(text or ''), flags=re.UNICODE)


def _variant_matches_tokens(tokens, variant):
    variant = search_fold(str(variant))
    if ' ' in variant:
        haystack = ' '.join(tokens)
        return bool(re.search(r'(?<!\w)' + re.escape(variant) + r'(?!\w)', haystack))
    stem = search_stem_prefix(variant)
    return any(tok == variant or (stem != variant and tok.startswith(stem)) for tok in tokens)


def _term_positions(tokens, term, settings):
    positions = []
    variants = term_variants(term, settings)
    for i, token in enumerate(tokens):
        for variant in variants:
            if ' ' in variant:
                continue
            stem = search_stem_prefix(variant)
            if token == variant or (stem != variant and token.startswith(stem)):
                positions.append(i)
                break
    return positions


def lexical_score(text, terms, settings):
    tokens = _text_tokens(text)
    score = 0
    matched = 0
    details = []

    for term in terms:
        variants = term_variants(term, settings)
        direct = _variant_matches_tokens(tokens, str(term))
        alias = direct or any(_variant_matches_tokens(tokens, v) for v in variants[1:])
        if alias:
            matched += 1
            score += 4 if direct else 2
            details.append(str(term))

    # Бонус за близость терминов. Для многословного названия это резко
    # поднимает «Нова пошта» выше случайного текста, где слова разбросаны.
    if len(terms) >= 2 and matched == len(terms):
        tagged = []
        for term_index, term in enumerate(terms):
            for position in _term_positions(tokens, term, settings):
                tagged.append((position, term_index))
        tagged.sort()
        need = len(terms)
        have = Counter()
        covered = 0
        left = 0
        best_span = None
        for right, (pos, term_index) in enumerate(tagged):
            if have[term_index] == 0:
                covered += 1
            have[term_index] += 1
            while covered == need and left <= right:
                span = tagged[right][0] - tagged[left][0]
                best_span = span if best_span is None else min(best_span, span)
                left_term = tagged[left][1]
                have[left_term] -= 1
                if have[left_term] == 0:
                    covered -= 1
                left += 1
        if best_span is not None:
            if best_span <= len(terms) - 1:
                score += 14
            elif best_span <= len(terms) + 2:
                score += 8
            elif best_span <= len(terms) + 6:
                score += 3

    return matched, score, details


def minimum_term_matches(term_count, settings):
    if term_count <= 2:
        return term_count
    ratio = min(1.0, max(0.5, float(settings.get('search_partial_coverage', 0.67))))
    return max(2, min(term_count, int(math.ceil(term_count * ratio))))


_SEMANTIC_MODELS={}


def semantic_candidates(conn, rows, question, settings):
    """
    ML/embedding-поиск по Telegram-контенту намеренно отключён.

    Базовый тематический поиск работает локально через SQLite FTS5/LIKE
    и не требует модели машинного обучения.
    """
    return [], {
        "enabled": False,
        "used": False,
        "reason": "disabled_for_telegram_content_compliance",
    }


def search_database(conn, question, days, settings, channel_ids=None, limit_override=None):
    retention = max(1, int(settings.get("database_retention_days", 90)))
    requested = max(1, int(days))
    effective = min(requested, retention)
    cutoff = iso_utc(utc_now() - timedelta(days=effective))
    terms = extract_search_terms(question)
    if not terms:
        raise ValueError(
            "Введите тему, аббревиатуру (например ЕС) или фразу в кавычках."
        )

    requested_limit = int(
        settings.get("search_max_results", 1500)
        if limit_override is None
        else limit_override
    )
    limit = requested_limit if requested_limit > 0 else 2147483647
    candidate_limit = limit if requested_limit <= 0 else max(limit, min(3000, limit * 3))
    partial_candidate_limit = max(
        candidate_limit,
        int(settings.get('search_partial_candidate_limit', 10000)),
    )

    phrase_variants = query_phrase_variants(question, terms, settings)
    required_matches = minimum_term_matches(len(terms), settings)

    # 1) Строгое совпадение всех смысловых терминов — основной поиск.
    strict = run_fts_search(
        conn, cutoff, terms, settings, candidate_limit,
        operator="AND", channel_ids=channel_ids,
    )
    if strict is None:
        strict = run_like_search(
            conn, cutoff, terms, settings, candidate_limit,
            operator="AND", channel_ids=channel_ids,
        )

    # 2) Точная фраза/известный языковой вариант даёт бонус ранжированию.
    phrase = None
    if phrase_variants and fts5_is_available(conn):
        phrase_terms = [SearchPhrase(v) for v in phrase_variants]
        phrase = run_fts_search(
            conn, cutoff, phrase_terms, settings, candidate_limit,
            operator="OR", channel_ids=channel_ids,
        )

    records = {}

    def add_rows(search_result, source):
        if not search_result:
            return
        for row in search_result['rows']:
            key = (int(row['channel_id']), int(row['message_id']))
            rank = float(row['search_rank'] or 0.0)
            item = records.setdefault(key, {
                'row': row,
                'index_rank': rank,
                'sources': set(),
            })
            item['index_rank'] = min(item['index_rank'], rank)
            item['sources'].add(source)

    add_rows(strict, 'strict')
    add_rows(phrase, 'phrase')

    # 3) Контролируемое расширение допустимо только для 3+ терминов.
    # Для двухсловного запроса 1/2 совпадения никогда не считается результатом.
    broadened = False
    rejected_partial = 0
    partial_raw_total = 0
    if len(terms) >= 3:
        partial = run_fts_search(
            conn, cutoff, terms, settings, partial_candidate_limit,
            operator="OR", channel_ids=channel_ids,
        )
        if partial is None:
            partial = run_like_search(
                conn, cutoff, terms, settings, partial_candidate_limit,
                operator="OR", channel_ids=channel_ids,
            )
        partial_raw_total = int(partial['total'] or 0)
        for row in partial['rows']:
            matched, _, _ = lexical_score(row['text'] or '', terms, settings)
            if matched < required_matches:
                rejected_partial += 1
                continue
            key = (int(row['channel_id']), int(row['message_id']))
            if key not in records:
                broadened = True
            add_rows({'rows': [row]}, 'coverage')

    ranked = []
    for key, item in records.items():
        row = item['row']
        matched, direct_score, matched_terms = lexical_score(row['text'] or '', terms, settings)
        # Защитный фильтр: даже после FTS/LIKE двухсловный запрос обязан иметь 2/2.
        if matched < required_matches:
            continue
        phrase_hit = 'phrase' in item['sources']
        if phrase_hit:
            direct_score += 18
        coverage = matched / max(1, len(terms))
        ranked.append((
            -coverage,
            -direct_score,
            item['index_rank'],
            row['date_utc'] or '',
            row,
            matched,
            direct_score,
            matched_terms,
            phrase_hit,
            sorted(item['sources']),
        ))

    # Сначала полнота запроса и содержательный балл, затем BM25 и свежесть.
    ranked.sort(key=lambda item: item[3], reverse=True)
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))

    lexical_total = len(ranked)
    selected_ranked = ranked[:limit]

    direct = []
    seen_keys = set()
    for _, _, index_rank, _, row, matched, direct_score, matched_terms, phrase_hit, sources in selected_ranked:
        message = db_row_to_message(row, None)
        message.pop("change_status", None)
        key = (int(row['channel_id']), int(row['message_id']))
        seen_keys.add(key)
        message["search_match"] = {
            "kind": "indexed_lexical",
            "matched_terms": matched,
            "total_terms": len(terms),
            "term_coverage": round(matched / max(1, len(terms)), 3),
            "matched_term_names": matched_terms,
            "phrase_match": phrase_hit,
            "sources": sources,
            "direct_score": direct_score,
            "index_rank": index_rank,
        }
        direct.append(message)

    # 4) Совместимый остаток старого смыслового резерва.
    # Начиная с 5.4.13 semantic_enabled принудительно False, поэтому Telegram-контент
    # обрабатывается только локальным FTS5/LIKE-поиском без ML-модели.
    semantic_meta = {
        'enabled': bool(settings.get('semantic_enabled', False)),
        'used': False,
    }
    semantic_added = 0
    semantic_trigger = max(0, int(settings.get('semantic_trigger_below', 8)))
    if settings.get('semantic_enabled', False) and lexical_total < semantic_trigger:
        channel_sql = ''
        params = [cutoff]
        if channel_ids:
            ids = [int(x) for x in channel_ids]
            channel_sql = ' AND channel_id IN (' + ','.join('?' for _ in ids) + ')'
            params.extend(ids)
        max_semantic_rows = max(1, int(settings.get('semantic_max_messages', 20000)))
        rows = conn.execute(
            'SELECT * FROM messages WHERE date_utc >= ?' + channel_sql +
            ' ORDER BY date_utc DESC LIMIT ?',
            (*params, max_semantic_rows),
        ).fetchall()
        semantic_hits, semantic_meta = semantic_candidates(conn, rows, question, settings)
        semantic_meta['used'] = True
        for row, similarity in semantic_hits:
            key = (int(row['channel_id']), int(row['message_id']))
            if key in seen_keys:
                continue
            if len(direct) >= limit:
                break
            message = db_row_to_message(row, None)
            message.pop("change_status", None)
            message['search_match'] = {
                'kind': 'semantic',
                'semantic_score': round(float(similarity), 4),
                'note': 'Смысловое совпадение: используйте как контекст, а не как буквальное совпадение запроса.',
            }
            direct.append(message)
            seen_keys.add(key)
            semantic_added += 1
    elif settings.get('semantic_enabled', False):
        semantic_meta.update({
            'used': False,
            'reason': 'Лексический поиск дал достаточно результатов; смысловой резерв не запускался.',
        })
    else:
        semantic_meta.update({
            'reason': 'disabled_in_current_version',
            'hint': 'Поиск работает локально через SQLite FTS5/LIKE без ML-модели.',
        })

    direct = attach_versions(
        conn,
        direct,
        max(0, int(settings.get("revision_export_limit", 3))),
    )
    related_groups = build_related_groups(direct)

    counts = Counter(m["channel"] for m in direct if m.get("channel"))
    dates = [m["date_utc"] for m in direct if m.get("date_utc")]
    method = strict['method']
    if broadened:
        method += '_coverage_fallback'
    if semantic_added:
        method += '+semantic'

    # Если OR-кандидатов было больше, чем мы физически просмотрели, явно помечаем это.
    partial_pool_truncated = bool(
        len(terms) >= 3 and partial_raw_total > partial_candidate_limit
    )
    overall_total = lexical_total + semantic_added

    return {
        "question": question.strip(),
        "requested_days": requested,
        "effective_days": effective,
        "database_retention_days": retention,
        "cutoff_utc": cutoff,
        "terms": [str(term) for term in terms],
        "phrase_variants": phrase_variants,
        "minimum_term_matches": required_matches,
        "method": method,
        "engine_query": strict.get("query"),
        "broadened_to_or": broadened,
        "partial_raw_candidates": partial_raw_total,
        "rejected_partial_candidates": rejected_partial,
        "partial_pool_truncated": partial_pool_truncated,
        "total_direct_hits": overall_total,
        "total_lexical_hits": lexical_total,
        "total_semantic_hits": semantic_added,
        "truncated": overall_total > len(direct) or partial_pool_truncated,
        "direct_results": direct,
        "related_context": [],
        "related_message_groups": related_groups,
        "channels_with_direct_hits": [
            {"channel": name, "matches": count}
            for name, count in counts.most_common()
        ],
        "earliest_direct_match_utc": min(dates) if dates else None,
        "latest_direct_match_utc": max(dates) if dates else None,
        "semantic": semantic_meta,
    }

def local_history_status(conn, channels, days, settings):
    now = utc_now()
    retention = max(1, int(settings.get('database_retention_days', 90)))
    effective = min(max(1/24, float(days)), retention)
    cutoff = now - timedelta(days=effective)
    freshness = max(0, float(settings.get('search_freshness_minutes', 15))) * 60
    stale = []; covered = 0; entries = []
    for ch in channels:
        state = get_channel_state(conn, ch['id']) or {}
        last = parse_dt(state.get('last_success_utc'))
        start = parse_dt(state.get('history_coverage_utc'))
        fresh = bool(last and 0 <= (now-last).total_seconds() <= freshness and not state.get('last_error'))
        complete = bool(start and start <= cutoff)
        covered += int(complete)
        if not fresh: stale.append(ch['id'])
        entries.append(dict(channel=ch['name'], complete=complete, fresh=fresh,
                            last_success_utc=state.get('last_success_utc'), coverage_utc=state.get('history_coverage_utc')))
    return dict(complete=bool(channels) and covered==len(channels) and not stale and float(days)<=retention,
                complete_channels=covered, selected_channels=len(channels), stale_channel_ids=stale,
                channel_coverage=entries, mode='local_snapshot', freshness_minutes=freshness/60,
                warning='Локальный снимок: новые публикации после последней синхронизации могут отсутствовать.')


def print_database_status(conn, channels):
    print('\nСОСТОЯНИЕ БАЗЫ (без обращения к Telegram)')
    for channel in channels:
        state = get_channel_state(conn, channel['id']) or {}
        bounds = channel_db_bounds(conn, channel['id'])
        print(channel['name'] + ': последнее успешное обновление ' +
              str(state.get('last_success_utc') or 'не выполнялось') + ' (UTC)')
        print('  Сохранённые публикации: ' + str(bounds.get('earliest_utc')) +
              ' — ' + str(bounds.get('latest_utc')))
        if state.get('last_error'):
            print('  Ошибка: ' + str(state['last_error']))
    print('Правки проверяются за последние 2 часа, максимум 50 сообщений на канал. Текст на изображениях не распознаётся.')


def setup_semantic_search():
    """Старый CLI-флаг сохранён только ради понятного сообщения."""
    print(
        "\nСмысловой ML-поиск отключён в этой версии. "
        "Поиск по Telegram-контенту работает локально через SQLite FTS5/LIKE."
    )
    return False



async def main():
    with InstanceLock():
        setup_logging()
        print('\n'+APP_DISPLAY_NAME+' '+APP_VERSION)
        if '--setup-semantic' in sys.argv:
            setup_semantic_search()
            return
        await _v4_main()


def write_preview(payload, json_path, settings):
    """Standalone local HTML. Untrusted posts are rendered only with textContent."""
    data=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    page=PREVIEW_HTML.replace('__PAYLOAD__',data)
    destination=Path(json_path).with_suffix('.html')
    temporary=destination.with_suffix('.html.tmp')
    temporary.write_text(page,encoding='utf-8')
    os.replace(temporary,destination)
    if settings.get('open_html_preview',True) and os.name=='nt':
        try: os.startfile(str(destination))
        except OSError as e: log_error('Preview: '+str(e))
    return destination


def save_output(*args, **kwargs):
    result=_v4_save_output(*args,**kwargs)
    settings=kwargs.get('settings') or args[-1]
    if settings.get('open_html_preview',False):
        payload=json.loads(result[0].read_text(encoding='utf-8'))
        try: write_preview(payload,result[0],settings)
        except Exception as e: log_error('HTML preview failed (JSON saved): '+str(e))
    return result


def save_search_output(conn, question, days, settings, db_maintenance, channels=None, history_status=None, search_result=None):
    result=_v4_save_search_output(conn,question,days,settings,db_maintenance,channels,history_status,search_result)
    if settings.get('open_html_preview',False):
        try: write_preview(result[2],result[0],settings)
        except Exception as e: log_error('HTML preview failed (JSON saved): '+str(e))
    return result


PREVIEW_HTML=r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>Unofficial TelegramNewsAI · Просмотр</title><style>
:root{color-scheme:light;--ink:#1c293d;--muted:#607086;--blue:#2463ac;--line:#dce5ef}
*{box-sizing:border-box}body{margin:0;background:#f3f6fa;color:var(--ink);font:16px/1.6 'Segoe UI',Arial,sans-serif}
header{background:#142a43;color:white;padding:30px max(24px,calc((100vw - 1100px)/2));border-bottom:5px solid #64b4d4}
.eyebrow{color:#a9c8e6;letter-spacing:.16em;font-size:12px}h1{font-size:30px;margin:6px 0}header p{margin:0;color:#cbd9e8}
main{max-width:1148px;margin:24px auto;padding:0 24px}.status{padding:15px 20px;border-radius:12px;background:#e2f2e9;color:#185436;margin-bottom:20px}
.status.warn{background:#fff0d7;color:#774716}.toolbar{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px;background:white;padding:18px;border:1px solid var(--line);border-radius:14px}
label{font-size:12px;font-weight:600;color:var(--muted)}input,select{display:block;width:100%;padding:11px;border:1px solid #b9c9db;border-radius:7px;background:white;color:var(--ink);font:14px 'Segoe UI'}
#count{color:var(--muted);margin:18px 0}article{background:white;border:1px solid var(--line);border-radius:14px;padding:22px;margin:15px 0;box-shadow:0 3px 10px #13294505}
.meta{display:flex;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:13px}.channel{font-weight:700;color:var(--blue)}.body{white-space:pre-wrap;overflow-wrap:anywhere;margin:14px 0}
.badge{background:#eaf1fb;border-radius:6px;padding:2px 8px;font-size:12px}.unavailable{background:#ffebe8;color:#933}
a{color:var(--blue)}details{border-top:1px solid var(--line);margin-top:13px;padding-top:10px}summary{cursor:pointer;color:var(--blue);font-size:14px}
.version{background:#f4f7fb;padding:12px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}.note{color:var(--muted);font-size:13px}
button{border:1px solid #b9c9db;border-radius:8px;padding:10px 18px;background:white;color:var(--blue);cursor:pointer}footer{padding:28px 0;color:var(--muted);font-size:12px}
@media(max-width:750px){.toolbar{grid-template-columns:1fr 1fr}header{padding:22px}h1{font-size:24px}main{padding:0 14px}article{padding:16px}}
@media print{.toolbar,button{display:none}header{background:white;color:black}article{break-inside:avoid}}
</style></head><body><header><div class="eyebrow" id="eyebrow">UNOFFICIAL TELEGRAMNEWSAI</div><h1 id="title">Лента публикаций</h1><p id="subtitle">Публикации, источники и история изменений</p></header>
<main><div id="quality" class="status"></div><details id="coverage"><summary>Полнота истории по каналам</summary><div id="coverageBody"></div></details>
<div class="toolbar"><label>Найти в результатах<input id="query" placeholder="Слово, имя или фраза"></label><label>Канал<select id="channel"><option value="">Все каналы</option></select></label>
<label>Состояние<select id="state"><option value="">Все сообщения</option><option value="new">Новые</option><option value="edited">Исправленные</option><option value="unavailable">Недоступные</option><option value="operational">Оперативные</option><option value="semantic">По смыслу</option></select></label>
</div>
<p id="count" aria-live="polite"></p><section id="cards"></section><button id="more" hidden>Показать ещё 100</button>
<footer>Локальный просмотр публикаций. Сходство и число перепечаток не являются независимым подтверждением сообщения.</footer></main>
<script id="payload" type="application/json">__PAYLOAD__</script><script>
'use strict';
const payload=JSON.parse(document.getElementById('payload').textContent),meta=payload.meta||{};
const $=id=>document.getElementById(id);
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n};
$('eyebrow').textContent='UNOFFICIAL TELEGRAMNEWSAI'+(meta.collector_version?' · '+meta.collector_version:'');
const messages=[...(payload.news_messages||[]),...(payload.operational_messages||[]).map(m=>({...m,operational:true})),...(payload.search_results||[]),...(payload.related_context||[])];
const history=meta.history_completeness||{},quality=$('quality');
quality.classList.toggle('warn',history.complete!==true);
quality.textContent=history.complete===true?'История за выбранный период загружена полностью.':'История неполная или её полнота не проверена. Отсутствие сообщения в выдаче не означает отсутствие события.';
if(meta.search_engine?.truncated)quality.append(node('div','Выдача ограничена: найдено '+meta.search_engine.total_direct_hits+', выгружено '+meta.search_engine.exported_direct_hits+'.'));
if(meta.search_engine?.semantic?.truncated)quality.append(node('div','Поиск по смыслу проверил только часть архива: '+meta.search_engine.semantic.indexed+' сообщений.'));
if(meta.search_engine?.semantic?.error)quality.append(node('div','Поиск по смыслу недоступен; показаны совпадения по словам.'));
if(meta.search_engine?.broadened_to_or)quality.append(node('div','Добавлены частичные совпадения, прошедшие порог покрытия терминов; одиночные случайные слова отброшены.'));
if(payload.search_overview){$('title').textContent='Поиск: '+payload.search_overview.question;$('subtitle').textContent='За '+payload.search_overview.period_days+' дн. · '+(meta.created_local||'');}
else $('subtitle').textContent='За '+(meta.hours||'?')+' ч. · '+(meta.created_local||'');
for(const c of history.channel_coverage||[])$('coverageBody').append(node('p',c.channel+' — '+(c.complete?'период загружен':'неполно')+(c.error?': '+c.error:''),'note'));
if(!(history.channel_coverage||[]).length)$('coverage').hidden=true;
for(const ch of [...new Set(messages.map(m=>m.channel||'Без названия'))].sort()){$('channel').append(node('option',ch));}
function kind(m){if(m.availability==='unavailable')return 'unavailable';if((m.change_status||'').startsWith('edited'))return 'edited';if((m.change_status||'').startsWith('new'))return 'new';return '';}
const labels={new:'Новое',edited:'Исправлено',unavailable:'Недоступно в Telegram'};
function detail(parent,label,text){const d=node('details'),s=node('summary',label);d.append(s,node('div',text,'version'));parent.append(d);}
function sourceNode(text,value){try{const url=new URL(value);if(url.protocol==='https:'&&url.hostname==='t.me'){const link=node('a',text,'channel');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';return link;}}catch(e){}return node('span',text,'channel');}
function card(m){const a=node('article'),top=node('div',undefined,'meta');top.append(sourceNode(m.channel||'Источник',m.telegram_url||m.channel_url),node('span',m.date_local||m.date_utc||''));const k=kind(m);if(k)top.append(node('span',labels[k],'badge '+k));if(m.operational)top.append(node('span','Оперативное','badge'));if(m.search_match?.kind==='semantic')top.append(node('span','По смыслу','badge'));a.append(top,node('div',m.text||'[Без текста]','body'));
if(m.version_note)a.append(node('p',m.version_note,'note'));
if(m.raw_text_available===false)a.append(node('p','Оригинал до очистки ещё не получен: запись создана старой версией.','note'));
else detail(a,'Исходный текст до очистки',m.raw_text??m.text??'[Публикация без подписи]');
for(const v of m.previous_versions||[])detail(a,'Предыдущая редакция · сохранена '+v.captured_utc,(v.raw_text===null?'[Оригинал до очистки не сохранён]\n':'')+(v.raw_text??v.text??''));
if(m.versions_truncated)a.append(node('p','Показаны последние редакции; остальные сохранены в базе.','note'));
if((m.duplicates||[]).length){const d=node('details');d.append(node('summary','Точные повторы: '+m.duplicates.length));for(const dup of m.duplicates)d.append(card({...dup,duplicates:[]}));a.append(d);}
return a;}
let shown=100,filtered=[];
function render(reset=true){if(reset)shown=100;const q=$('query').value.toLocaleLowerCase(),ch=$('channel').value,state=$('state').value;filtered=messages.filter(m=>(!q||(m.text||'').toLocaleLowerCase().includes(q))&&(!ch||m.channel===ch)&&(!state||kind(m)===state||(state==='operational'&&m.operational)||(state==='semantic'&&m.search_match?.kind==='semantic')));$('cards').replaceChildren(...filtered.slice(0,shown).map(card));$('count').textContent='Показано '+Math.min(shown,filtered.length)+' из '+filtered.length+' · всего '+messages.length;$('more').hidden=shown>=filtered.length;if(!filtered.length)$('cards').append(node('p','Совпадений с выбранными фильтрами нет.'));}
for(const id of ['query','channel','state'])$(id).addEventListener('input',()=>render());$('more').addEventListener('click',()=>{shown+=100;render(false)});render();
</script></body></html>'''


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        log_info("STOPPED BY USER")

    except Exception as e:
        print("\nОШИБКА:")
        print(e)
        print(
            "\nСделайте снимок экрана "
            "и пришлите его."
        )

        try:
            LOGGER.exception(
                "UNHANDLED ERROR"
            )
        except Exception:
            pass

    finally:
        try:
            input(
                "\nНажмите Enter для выхода..."
            )
        except (EOFError, KeyboardInterrupt):
            pass
