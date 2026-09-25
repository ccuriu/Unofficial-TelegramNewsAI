# Telegram API — принятый профиль испытаний 5.5.0 Stable

Status: **ACCEPTED HISTORICAL TEST RECORD**. Этот документ больше не является списком незавершённых работ. Текущий статус проекта хранится в `PROJECT_STATE.md`.

## Поддерживаемый масштаб

Unofficial TelegramNewsAI поддерживает максимум **50 выбранных каналов/источников**. Это продуктовый предел проекта, а не официальный safe limit Telegram. Telegram не публикует универсального безопасного числа каналов для сторонних MTProto/API-клиентов.

51-й источник отклоняется локально до дополнительной сетевой работы. Испытания 60/75/100 источников не являются условием Stable.

## Принятый защитный профиль

- `Telethon.flood_sleep_threshold = 0`;
- первый FloodWait прекращает соответствующий сетевой этап;
- PEER_FLOOD, revoked/expired session, frozen/banned и сходные account-level сигналы не ретраятся вслепую;
- скрытая повторная авторизация запрещена;
- запросы истории идут последовательно;
- обычный запуск сначала использует локальный entity-cache;
- штатная межканальная пауза — 1,0 с; это инженерный default, не Telegram guarantee;
- обычный sync пишет локальный `API_SAFETY_SUMMARY`;
- `history_iterators_started` не выдаётся за точное число внутренних MTProto requests/pages;
- две обычные копии программы одновременно блокируются named mutex.

## Реальная приёмка

Принятый 50-source RUN 3 завершён без наблюдавшихся FloodWait, network retry, account/session safety signal или принудительной повторной авторизации. SQLite `PRAGMA quick_check` после принятого прогона — `ok`.

Этот результат подтверждает поддерживаемый сценарий проекта на момент теста, но не является обещанием будущего поведения Telegram и не отменяет hard-stop правила.

## Данные и языки

Русский, украинский и английский текст проходит Telegram → SQLite → экспорт без обязательного встроенного перевода. Локальный FTS5/LIKE и continuity-context работают с исходным текстом. Встроенный тяжёлый ML/embedding-слой не требуется для принятой Stable baseline.

## Что делать при защитном сигнале

Первый FloodWait, PEER_FLOOD, потеря/отзыв сессии, ограничение аккаунта или сходный сигнал прекращает соответствующий сетевой этап. Не повышать параллелизм, не обходить антиспам-механизмы и не повторять нагрузочный тест без конкретной диагностической причины.

## Источники

- https://core.telegram.org/api/obtaining_api_id
- https://core.telegram.org/api/errors
- https://core.telegram.org/api/auth
- https://core.telegram.org/method/messages.getHistory
- https://docs.telethon.dev/en/stable/modules/client.html
- https://docs.telethon.dev/en/stable/modules/sessions.html
