# -*- coding: utf-8 -*-
"""TelegramNewsAI — локальный сбор, история и поиск Telegram-публикаций.
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
from collections import Counter
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from getpass import getpass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.tl.types import Channel
except ImportError:
    print("Telethon не установлен.")
    print("Выполните: python -m pip install --upgrade telethon")
    input("Нажмите Enter для выхода...")
    raise SystemExit(1)

APP_VERSION = "5.4.6 Stable"

# Версии экспортируемого JSON независимы от версии приложения.
# Меняются только при несовместимом изменении контракта или инструкций.
EXPORT_SCHEMA_VERSION = 6
DIGEST_PROFILE_VERSION = "6.0"

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
SEARCH_LATEST_FILE = OUTPUT_DIR / "ПОИСК_ПОСЛЕДНИЙ.json"
SEARCH_ARCHIVE_DIR = ARCHIVE_DIR / "Поиск"


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
    "max_flood_wait_seconds": 90,
    # Фиксируем поведение Telethon явно: ожидания до этого порога
    # библиотека пережидает сама, более длинные попадают в наш retry-код.
    "telethon_flood_sleep_threshold_seconds": 60,

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

    # Смысловой поиск опционален. После --setup-semantic он используется
    # только как резерв, если лексический поиск дал мало результатов.
    "semantic_enabled": False,
    "semantic_model": "intfloat/multilingual-e5-small",
    "semantic_max_messages": 20000,
    "semantic_max_results": 60,
    "semantic_min_score": 0.78,
    "semantic_trigger_below": 8
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

    # Эти ограничения не дают обычному поиску повторно сканировать Telegram
    # и загружать тяжёлую нейронную модель без явной необходимости.
    result.update({
        "search_related_context_limit": 0,
        "deletion_check_limit": 0,
        "open_html_preview": False,
        "refresh_recent_messages": max(0, min(100, int(result.get("refresh_recent_messages", 50)))),
        "refresh_recent_hours": max(0, float(result.get("refresh_recent_hours", 2))),
    })

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

    logger = logging.getLogger("TelegramNewsAI")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


LOGGER = logging.getLogger("TelegramNewsAI")


def log_info(message):
    LOGGER.info(message)


def log_error(message):
    LOGGER.error(message)


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


def load_or_create_credentials():
    if CRED_FILE.exists():
        try:
            raw = dpapi_decrypt(CRED_FILE.read_bytes())
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"Не удалось прочитать credentials.bin: {e}")
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_file = CRED_FILE.with_name(
                f"credentials.unreadable-{timestamp}.bin"
            )
            CRED_FILE.replace(backup_file)
            print(
                "Старые данные авторизации не удалены и сохранены в "
                f"{backup_file.name}."
            )
            print("Сейчас нужно один раз заново ввести данные Telegram API.")

    print("\n=== Первичная настройка Telegram ===")
    print("Для подключения нужны API ID и API Hash.")
    print(
        "Получить их можно: "
        "https://my.telegram.org → API development tools."
    )
    print("API ID — число, API Hash — строка букв и цифр.\n")

    api_id_text = input("Введите API ID: ").strip()
    if not api_id_text.isdigit():
        raise ValueError("API ID должен состоять из цифр.")

    print(
        "\nAPI Hash вводится скрыто: "
        "символы на экране не отображаются — это нормально."
    )
    api_hash = getpass(
        "Введите или вставьте API Hash и нажмите Enter: "
    ).strip()
    phone = input(
        "\nВведите номер Telegram в международном формате, "
        "например +380...: "
    ).strip()

    data = {
        "api_id": int(api_id_text),
        "api_hash": api_hash,
        "phone": phone,
    }

    CRED_FILE.write_bytes(
        dpapi_encrypt(
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        )
    )
    return data


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
    print("\nМожно добавить публичные каналы БЕЗ подписки.")
    print("Поддерживаются:")
    print("  @truexanewsua")
    print("  https://t.me/truexanewsua")
    print("  https://t.me/truexanewsua/12345")
    print("Несколько адресов — через запятую.")

    raw = input(
        "Ссылки/@имена [Enter=ничего не добавлять]: "
    ).strip()

    if not raw:
        return items

    added = 0

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue

        try:
            item = await resolve_public_channel(client, part)

            if any(
                int(x["id"]) == int(item["id"])
                for x in items
            ):
                print(f"  Уже есть: {item['name']}")
                continue

            items.append(item)
            added += 1

            uname = item.get("username") or "без username"
            print(f"  Добавлен: {item['name']} (@{uname})")

        except Exception as e:
            print(f"  Не добавлен {part}: {e}")

    items = dedupe_channel_items(items)

    if added:
        save_selection(items)
        print(f"Добавлено новых каналов: {added}")

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


def parse_number_selection(raw, maximum):
    selected = set()
    raw = (raw or "").replace(" ", "")

    if not raw:
        return selected

    for part in raw.split(","):
        if not part:
            continue

        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)

            for n in range(min(a, b), max(a, b) + 1):
                if 1 <= n <= maximum:
                    selected.add(n)
        else:
            n = int(part)
            if 1 <= n <= maximum:
                selected.add(n)

    return selected


def prompt_remove_channels(items):
    if not items:
        print("\nСписок уже пуст.")
        return items

    print_selected_channels(items)
    print("\nВведите номера каналов, которые нужно удалить.")
    print("Пример: 2,5,8-10")

    raw = input(
        "Удалить [Enter=отмена]: "
    ).strip()

    if not raw:
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
    print("Пример: 1,3,7-12")
    print("Можно написать all.")

    raw = input("Выбор: ").strip().lower()
    selected_nums = set()

    if raw == "all":
        selected_nums = set(
            range(1, len(subscribed) + 1)
        )
    else:
        try:
            selected_nums = parse_number_selection(
                raw,
                len(subscribed),
            )
        except Exception:
            print("Не удалось разобрать выбор.")
            selected_nums = set()

    items = [
        channel_item(
            subscribed[i - 1].entity,
            subscribed[i - 1].name,
        )
        for i in sorted(selected_nums)
        if 1 <= i <= len(subscribed)
    ]

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

    # Если сохранённого списка ещё нет, сначала создаём его.
    if not restored:
        restored = await select_from_subscriptions(
            client,
            subscribed,
        )

        if not restored:
            return []

        # При обычном дайджесте сразу продолжаем с новым списком.
        if skip_menu and not initial_action:
            return restored

    # Обычный дайджест: никаких лишних меню.
    if skip_menu and not initial_action:
        return restored

    pending_action = (
        str(initial_action).strip().lower()
        if initial_action
        else None
    )

    while True:
        if pending_action:
            ans = pending_action
            pending_action = None
        else:
            print(
                f"\nСохранённый список: "
                f"{len(restored)} каналов."
            )
            print("Enter — продолжить / выйти из управления")
            print("A     — добавить публичный канал")
            print("R     — удалить канал")
            print("L     — показать текущий список")
            print("N     — создать список заново")

            ans = input(
                "Ваш выбор [Enter/A/R/L/N]: "
            ).strip().lower()

        if ans in ("", "enter"):
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
            restored = await select_from_subscriptions(
                client,
                subscribed,
            )
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
    r"^\s*труха.*надіслати новину\s*$",
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

    return {
        k: v
        for k, v in result.items()
        if v is not None
    } or None


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

    if canonical_urls:
        return "url:" + canonical_urls[0]

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
        "forwarded_from": message.get(
            "forwarded_from"
        ),
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

        "forwarded_from": json_loads(
            row["forwarded_from_json"],
            None,
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
            async for msg in client.iter_messages(
                channel["entity"]
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
            async for msg in client.iter_messages(
                channel["entity"],
                min_id=last_message_id,
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

        # Небольшое окно последних постов обновляем повторно.
        # Это нужно, чтобы поймать edit_date, просмотры,
        # реакции и число комментариев без повторного чтения всей истории.
        refresh_limit = int(
            settings.get(
                "refresh_recent_messages",
                80,
            )
        )

        refresh_cutoff = utc_now() - timedelta(hours=float(settings.get('refresh_recent_hours', 2)))
        if refresh_limit > 0:
            async for msg in client.iter_messages(
                channel["entity"],
                limit=refresh_limit,
            ):
                refresh_hours = float(settings.get('refresh_recent_hours', 2))
                if refresh_hours > 0 and getattr(msg, 'date', None) and msg.date < refresh_cutoff:
                    break
                # Do not advance the incremental cursor here: a publication
                # arriving during refresh must not hide other concurrent posts.
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

    max_flood_wait = max(
        0,
        int(
            settings.get(
                "max_flood_wait_seconds",
                90,
            )
        ),
    )

    last_error = None

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
            )
            return result

        except FloodWaitError as e:
            last_error = (
                f"FloodWait {e.seconds} сек."
            )

            if (
                e.seconds <= max_flood_wait
                and attempt < attempts
            ):
                print(
                    f"    Telegram просит подождать "
                    f"{e.seconds} сек.; повторю..."
                )
                log_info(
                    f"{channel['name']}: "
                    f"{last_error}"
                )
                await asyncio.sleep(
                    e.seconds + 1
                )
                continue

            break

        except Exception as e:
            last_error = (
                f"{type(e).__name__}: {e}"
            )

            log_error(
                f"{channel['name']} | "
                f"попытка {attempt}/{attempts} | "
                f"{last_error}"
            )

            if attempt < attempts:
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
    }


async def sync_all_channels(
    client,
    conn,
    channels,
    hours,
    settings,
):
    overall = init_sync_stats()

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

    max_flood_wait = max(
        0,
        int(
            settings.get(
                "max_flood_wait_seconds",
                90,
            )
        ),
    )

    last_error = None

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

            if (
                e.seconds <= max_flood_wait
                and attempt < attempts
            ):
                print(
                    f"      Telegram просит подождать "
                    f"{e.seconds} сек.; повторю..."
                )
                await asyncio.sleep(
                    e.seconds + 1
                )
                continue

            break

        except Exception as e:
            last_error = (
                f"{type(e).__name__}: {e}"
            )

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

    print(
        f"\nПроверяю полноту истории за "
        f"{effective_days} дней..."
    )

    results = []

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
    Связывает разные посты, которые ведут на один источник
    или являются пересылкой одного Telegram-поста.
    Тексты не удаляются: это именно группировка, а не дедупликация.
    """
    parent = {}
    key_to_message = {}

    for message in messages:
        key = message_key(message)
        parent[key] = key
        key_to_message[key] = message

    def find(x):
        while parent[x] != x:
            parent[x] = parent[
                parent[x]
            ]
            x = parent[x]
        return x

    def union(a, b):
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    buckets = {}

    for message in messages:
        key = message_key(message)
        relation_keys = []

        origin_key = message.get(
            "origin_key"
        )
        if origin_key:
            relation_keys.append(
                "origin:" + origin_key
            )

        for url in message.get(
            "canonical_urls",
            [],
        ):
            relation_keys.append(
                "url:" + url
            )

        for relation_key in relation_keys:
            previous_key = buckets.get(
                relation_key
            )

            if previous_key:
                union(key, previous_key)
            else:
                buckets[
                    relation_key
                ] = key

    groups = {}

    for key in parent:
        root = find(key)
        groups.setdefault(
            root,
            [],
        ).append(key)

    output = []
    group_index = 1

    for keys in groups.values():
        if len(keys) < 2:
            continue

        members = [
            key_to_message[key]
            for key in keys
        ]

        channels = unique_keep_order(
            m.get("channel")
            for m in members
        )

        canonical_urls = unique_keep_order(
            url
            for m in members
            for url in m.get(
                "canonical_urls",
                [],
            )
        )

        origin_keys = unique_keep_order(
            m.get("origin_key")
            for m in members
            if m.get("origin_key")
        )

        group_id = (
            f"related_{group_index:04d}"
        )
        group_index += 1

        for message in members:
            message[
                "related_group_id"
            ] = group_id

        output.append({
            "group_id": group_id,
            "message_refs": [
                {
                    "message_key": message_key(m),
                    "channel": m.get("channel"),
                    "username": m.get("username"),
                    "message_id": m.get(
                        "message_id"
                    ),
                    "channel_url": m.get(
                        "channel_url"
                    ) or public_channel_link(m.get("username")),
                    "telegram_url": m.get(
                        "telegram_url"
                    ),
                }
                for m in members
            ],
            "channels": channels,
            "canonical_urls": canonical_urls,
            "origin_keys": origin_keys,
            "note": (
                "Сообщения связаны общим исходным Telegram-постом "
                "или одной канонической внешней ссылкой. "
                "Разные формулировки сохранены отдельно."
            ),
        })

    return output




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
    "Для этого дайджеста игнорируй сведения о пользователе, персональную память, историю текущего и прошлых чатов, ранее "
    "обсуждавшиеся интересы, цели и предпочтения: они не являются источником фактов и не должны влиять на отбор, порядок, "
    "акценты или оценку полезности материала. Не пиши «для вас», «вам особенно важно» и подобные персональные оценки. "
    "Тематика заранее неизвестна: определяй темы, порядок и глубину только по фактическому материалу текущей выгрузки, без "
    "фиксированных рубрик и географических приоритетов. Объединяй связанные публикации в сюжеты, учитывай хронологию и поздние "
    "уточнения, не пересказывай сообщения по очереди. Различай прямое сообщение, независимое подтверждение, официальное "
    "заявление, пересказ названного первоисточника, инсайд, версию и собственный редакционный вывод. Если канал ссылается на "
    "Reuters, BBC, ведомство, конкретного человека или другой названный первоисточник, укажи это естественно. Не превращай "
    "утверждение в факт из-за статуса источника; важный одиночный инсайд можно включить с ясной атрибуцией. Расходящиеся версии "
    "сопоставляй без самовольного выбора победителя, а перепечатки одного исходного сообщения не считай независимыми "
    "подтверждениями. Не достраивай отсутствующие факты, включая автора действия, мотив, цель и причинность. Контекст или вывод "
    "добавляй только когда он прямо следует из материала и нужен для понимания; если факты самодостаточны, не дописывай "
    "обязательную аналитику и не ранжируй событие по значимости без опоры на материал. Внешние источники используй точечно для "
    "первоисточника, документа, точной цифры или необходимого контекста. Служебные поля используй только для внутреннего "
    "сопоставления; в готовом ответе их и технический процесс не показывай. Не обсуждай файл, JSON, локальную базу, "
    "синхронизацию, дедупликацию или алгоритм поиска. Пиши плотно и профессионально; точность важнее эффектности. Сохраняй "
    "имена, даты, числа и степень уверенности исходных сообщений. Заголовки делай короткими и не сильнее подтверждённых данных. "
    "Охвати все содержательно значимые сюжеты; второстепенные, но полезные события можно собрать компактно. "
)


SOURCE_RULES = (
    "Каждый самостоятельный фактический сюжет должен завершаться строкой источника, если доступен telegram_url или channel_url. "
    "Строка источника должна быть последней строкой сюжета: после неё не добавляй новых фактических утверждений. Если под одним "
    "заголовком объединены факты из разных публикаций, финальная строка источников должна покрывать все существенные "
    "утверждения; иначе раздели материал на отдельные сюжеты или пункты с собственными источниками. В блоке несвязанных коротких "
    "событий ставь источник непосредственно после каждого события и не собирай общий список ссылок в конце блока. Пункты "
    "«Главное за период» могут не дублировать ссылки, если эти сюжеты ниже имеют источники. При наличии telegram_url "
    "название канала делай единственной Markdown-ссылкой на конкретный пост; иначе используй channel_url, а при отсутствии обеих "
    "ссылок — обычное название. URL не придумывай. Для простого сюжета обычно достаточно одного содержательного источника; для "
    "составного используй 2–3 ключевые ссылки и не выдавай одну ссылку за подтверждение фактов, которых в ней нет. Не перечисляй "
    "одинаковые перепечатки. Формат: **Источник:** [Канал](url) или **Источники:** [Канал A](url) · [Канал B](url). "
)


DIGEST_REQUEST = (
    "Подготовь итоговый редакторский дайджест за выбранный период. У точных повторов inherited_fields восстанавливай из "
    "родительской публикации по inherits_from_message_key. Основной дайджест всегда строй по всему содержательному материалу "
    "текущей выгрузки из news_messages и operational_messages за выбранный период. changes_since_previous_digest — только "
    "дополнительный слой сравнения: он не задаёт временные границы основного дайджеста, не заменяет его и не является фильтром "
    "отбора. Если changes_since_previous_digest.comparison_available=true, используй его ссылки только для определения новых и "
    "содержательно изменённых публикаций и, при существенных изменениях, для отдельного блока «Что изменилось». Сообщения, уже "
    "присутствовавшие в предыдущем выпуске, не исключай из основного дайджеста, если они нужны для полной картины выбранного "
    "периода. Изменение одних просмотров, реакций, пересылок или комментариев новой новостью не считай. Используй "
    "related_message_groups для распознавания перепечаток и общего источника, сохраняя содержательные различия. "
    + EDITORIAL_PRINCIPLES +
    SOURCE_RULES +
    "В заголовке укажи дату и фактический локальный интервал всего охваченного материала по date_local, а не только сообщений "
    "из блока сравнения; если надёжно определить интервал нельзя, не придумывай. При насыщенном материале после заголовка сразу "
    "переходи к «Главное за период» из нескольких очень коротких пунктов; не ставь перед ним второй абзац с тем же резюме. Если "
    "важных событий мало, этот блок не нужен. Затем раскрой сюжеты по важности или естественной хронологии; глубину определяй "
    "количеством реально новой информации, обычно 1–3 компактными абзацами. Не повторяй подробно то, что уже сказано в кратком "
    "блоке. Однотипные оперативные предупреждения одного сюжета объединяй. Если последствия не подтверждены, сохраняй "
    "неопределённость. После «Главное за период» не добавляй повторный итог, личный выбор или рейтинг. «Что изменилось» не "
    "заменяет основной дайджест и добавляется только при доступном сравнении и существенных изменениях; открытые вопросы — только "
    "по материалу. Не объясняй читателю внутренние правила охвата и сравнения: молча применяй полный период основного выпуска "
    "и дополнительную роль блока изменений, не комментируя их в готовом тексте. Отсутствие новых сообщений не считай событием. "
)


def calculate_change_summary(messages):
    result = {
        "new_since_previous_digest": 0,
        "edited_since_previous_digest": 0,
        "metrics_changed_since_previous_digest": 0,
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
            "Это ссылки на реально новые и изменённые сообщения. "
            "Полный текст каждого сообщения хранится один раз в "
            "news_messages/operational_messages и находится по message_key."
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

            "raw_messages_in_period": len(
                raw_messages
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
            "recommended_ai_request": (
                DIGEST_REQUEST
            ),
        },

        "changes_since_previous_digest": (
            changes_block
        ),

        "related_message_groups": (
            related_groups
        ),

        "news_messages": news_messages,
        "operational_messages": (
            operational_messages
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


def build_search_ai_instruction(question, effective_days, search_result):
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
            "usage_hint": (
                "Передайте этот файл ИИ-ассистенту и попросите подготовить дайджест. "
                "Вопрос, период и редакционная инструкция уже записаны внутри файла."
            ),
            "recommended_ai_request": build_search_ai_instruction(
                result["question"],
                result["effective_days"],
                result,
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
        "search_results": result["direct_results"],
        "related_context": result["related_context"],
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

    question = input("\nЧто ищем: ").strip()

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

    raw_days = input(
        f"Период, дней [{default_days}; максимум {retention_days}]: "
    ).strip()

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
        update = input('Обновить устаревшие каналы и заполнить недостающий период? [Д/н]: ').strip().casefold()
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
        export_all = input("Выгрузить все результаты? [д/Н]: ").strip().casefold()
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

    print("\nФАЙЛ ДЛЯ ИИ-АССИСТЕНТА:")
    print(latest_path.name)

    print(
        "\nПосле загрузки достаточно написать: Дайджест"
    )
    print(
        "Вопрос, период и полнота истории уже записаны внутри JSON."
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

async def ensure_telegram_client(client, creds, settings=None):
    """
    Подключается к Telegram только когда это действительно нужно.
    Поиск по локальной news.db может работать вообще без Telegram.
    """
    if client is not None:
        try:
            if client.is_connected():
                return client, creds
        except Exception:
            pass

    if creds is None:
        creds = load_or_create_credentials()

    flood_sleep_threshold = max(
        0,
        min(
            86400,
            int((settings or DEFAULT_SETTINGS).get("telethon_flood_sleep_threshold_seconds", 60)),
        ),
    )

    client = TelegramClient(
        SESSION_FILE,
        creds["api_id"],
        creds["api_hash"],
        flood_sleep_threshold=flood_sleep_threshold,
    )

    print("\nПодключение к Telegram...")
    await client.start(
        phone=creds["phone"],
        code_callback=prompt_telegram_code,
        password=prompt_telegram_password,
    )
    print(
        "Telegram успешно подключён. "
        "Сессия сохранена — повторный ввод обычно не потребуется."
    )

    return client, creds


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
                "  A         — добавить канал"
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
            print(
                "  Q         — выйти"
            )

            mode = input(
                "Выбор [Enter/D/S/B/A/R/L/N/I/Q]: "
            ).strip().lower()

            if mode in ("i", "status"):
                print_database_status(conn, load_selection())
                continue

            if mode in ("q", "quit", "exit", "в", "выход"):
                print("\nВыход.")
                return

            if mode in ("s", "search", "п", "поиск"):
                channels = load_selection()

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
                client, creds = await ensure_telegram_client(
                    client,
                    creds,
                    settings,
                )

                await resolve_channels(
                    client,
                    initial_action=channel_action_aliases[mode],
                    return_after_initial=True,
                    skip_menu=False,
                )
                print(
                    "\nГотово. Изменения списка каналов сохранены."
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
                break

            print("\nНеизвестная команда. Выберите пункт из меню.")


        if not channels:
            print(
                "Не выбрано ни одного канала."
            )
            return

        default_hours = settings.get(
            "default_hours",
            24,
        )

        raw_hours = input(
            "\nЗа сколько последних часов "
            f"сделать выгрузку? "
            f"[{default_hours}]: "
        ).strip()

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

        news, operational = (
            split_operational(
                raw_messages,
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

        print(
            "\nФАЙЛ ДЛЯ ИИ-АССИСТЕНТА:"
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
            "\nПосле передачи файла ИИ-ассистенту "
            "попросите подготовить дайджест."
        )

        if settings.get(
            "open_output_folder",
            True,
        ):
            open_and_select_file(
                latest_path
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

class InstanceLock:
    """A kernel-owned byte lock, released even after a crash."""
    def __init__(self, path=None):
        self.path = Path(path or APP_DIR / 'collector.lock')
        self.file = None

    def __enter__(self):
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
            raise RuntimeError('Программа уже открыта. Завершите предыдущий сбор и повторите запуск.')
        return self

    def __exit__(self, *args):
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


def upsert_message(conn, message):
    key = (int(message['channel_id']), int(message['message_id']))
    old = conn.execute('SELECT * FROM messages WHERE channel_id=? AND message_id=?',key).fetchone()
    if old is not None and (old['text'] != message.get('text') or
        old['media_json'] != json_dumps(message.get('media',{})) or
        (old['raw_text'] is not None and old['raw_text'] != message.get('raw_text'))):
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
                item['previous_versions'].append(dict(captured_utc=row['captured_utc'], **json_loads(row['snapshot_json'],{})))
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
    cutoff=iso_utc(utc_now()-timedelta(hours=hours))
    baseline=previous_run_utc or '9999'
    rows=conn.execute('''SELECT * FROM messages WHERE channel_id IN ('''+','.join('?' for _ in ids)+''')
        AND (date_utc>=? OR content_changed_utc>? OR unavailable_since_utc>?) ORDER BY date_utc''',
        (*ids,cutoff,baseline,baseline)).fetchall()
    return attach_versions(conn,[db_row_to_message(r,previous_run_utc) for r in rows], max(0, int((settings or DEFAULT_SETTINGS).get('revision_export_limit', 3))))


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
                               m.get('availability'),m.get('change_status'),media_guard])
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
    ('новая','новой','новую','новые','новых','нова','нової','нову','нові','нових','nova'),
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
    if not settings.get('semantic_enabled',False):
        return [],{'enabled':False,'hint':'Для поиска по смыслу запустите enable_semantic.bat один раз.'}
    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
        name=settings.get('semantic_model','intfloat/multilingual-e5-small')
        if name not in _SEMANTIC_MODELS:
            # Downloads happen only via the explicit setup command.
            _SEMANTIC_MODELS[name]=SentenceTransformer(name,cache_folder=str(APP_DIR/'models'),local_files_only=True)
        model=_SEMANTIC_MODELS[name]
        maximum=max(1,int(settings.get('semantic_max_messages',20000)))
        chosen=rows[:maximum]
        vectors=[]
        print(f'Поиск по смыслу: проверка {len(chosen)} сообщений…')
        for start in range(0,len(chosen),32):
            batch=chosen[start:start+32]; batch_vectors={}; pending=[]
            for index,row in enumerate(batch):
                digest=hashlib.sha256((row['text'] or '').encode()).hexdigest()
                cached=conn.execute('SELECT vector FROM semantic_vectors WHERE channel_id=? AND message_id=? AND model=? AND content_hash=?',
                    (row['channel_id'],row['message_id'],name,digest)).fetchone()
                if cached:
                    batch_vectors[index]=np.frombuffer(cached[0],dtype=np.float32)
                else:
                    pending.append((index,row,digest))
            if pending:
                encoded=model.encode(['passage: '+(r['text'] or '') for _,r,_ in pending],normalize_embeddings=True,show_progress_bar=False)
                for (index,row,digest),vec in zip(pending,encoded):
                    vec=np.asarray(vec,dtype=np.float32); batch_vectors[index]=vec
                    conn.execute('INSERT OR REPLACE INTO semantic_vectors VALUES(?,?,?,?,?)',
                        (row['channel_id'],row['message_id'],name,digest,vec.tobytes()))
                conn.commit()
            vectors.extend(batch_vectors[i] for i in range(len(batch)))
        if not chosen: return [],{'enabled':True,'indexed':0}
        query=model.encode(['query: '+question],normalize_embeddings=True,show_progress_bar=False)[0]
        scores=np.asarray(vectors) @ query
        order=np.argsort(-scores)[:max(1,int(settings.get('semantic_max_results',60)))]
        threshold=float(settings.get('semantic_min_score',0.78))
        hits=[(chosen[int(i)],float(scores[int(i)])) for i in order if float(scores[int(i)])>=threshold]
        return hits,{'enabled':True,'model':name,'indexed':len(chosen),'available':len(rows),
                     'truncated':len(chosen)<len(rows),'threshold':threshold,
                     'note':'Сходство по смыслу — подсказка для поиска, не подтверждение факта.'}
    except Exception as e:
        log_error('Semantic search: '+str(e))
        print('Поиск по смыслу недоступен; продолжаю точный поиск. '+str(e))
        return [],{'enabled':True,'available':False,'error':str(e)}


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

    # 4) Опциональный смысловой резерв. Он не нужен для нормальной работы 5.4,
    # но после --setup-semantic помогает с синонимами и перефразировками.
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
            'reason': 'optional_not_enabled',
            'hint': 'Базовый поиск работает без модели. Для смыслового резерва запустите программу с --setup-semantic один раз.',
        })

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
        "related_message_groups": [],
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
    """Одноразово ставит модель и включает смысловой резерв в settings_free.json."""
    print('\nНастройка смыслового поиска. Это опционально: обычный поиск работает и без модели.')
    print('Устанавливаю sentence-transformers и загружаю multilingual-e5-small…')
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'sentence-transformers'])
    from sentence_transformers import SentenceTransformer
    settings = load_settings()
    model_name = settings.get('semantic_model', 'intfloat/multilingual-e5-small')
    SentenceTransformer(model_name, cache_folder=str(APP_DIR / 'models'))
    settings['semantic_enabled'] = True
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Готово. Смысловой поиск включён как резерв для слабой лексической выдачи.')


async def main():
    with InstanceLock():
        setup_logging()
        print('\nTelegramNewsAI '+APP_VERSION)
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
<title>TelegramNewsAI · Просмотр</title><style>
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
</style></head><body><header><div class="eyebrow" id="eyebrow">TELEGRAMNEWSAI</div><h1 id="title">Лента публикаций</h1><p id="subtitle">Публикации, источники и история изменений</p></header>
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
$('eyebrow').textContent='TELEGRAMNEWSAI'+(meta.collector_version?' · '+meta.collector_version:'');
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
if(m.raw_text_available)detail(a,'Исходный текст до очистки',m.raw_text||'[Публикация без подписи]');
else a.append(node('p','Оригинал до очистки ещё не получен: запись создана старой версией.','note'));
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
