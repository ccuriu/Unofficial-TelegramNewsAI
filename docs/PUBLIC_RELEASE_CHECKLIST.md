# Pre-public checklist — Unofficial TelegramNewsAI

Status: **PUBLIC RELEASE COMPLETED**.

Этот файл относится к открытию исходного GitHub-репозитория. Публичный бинарный Release/tag — отдельное решение. Актуальный технический статус всегда берётся из `PROJECT_STATE.md`.

## Уже принято

- [x] 5.5.0 Stable принят как рабочая baseline.
- [x] Максимум 50 выбранных источников закреплён до сетевого этапа.
- [x] Реальный 50-source safety gate пройден без FloodWait, повторной авторизации и потери сессии.
- [x] SQLite quick_check = ok.
- [x] Export schema 8 и digest profile 9.1 приняты regression-тестами.
- [x] Установка, clean install и in-place update проверены Windows CI.
- [x] Updater сохраняет session/credentials/settings/channels/database/history.
- [x] Пользовательское название: **Unofficial TelegramNewsAI**.
- [x] Иконка проекта оригинальная и не копирует официальный логотип Telegram.
- [x] Release builder использует имя `Unofficial-TelegramNewsAI-...`.
- [x] Программа не содержит встроенного AI API и не отправляет Telegram-контент во внешние AI/ML-сервисы автоматически.
- [x] Текущий `main` и CI не отслеживают session/credentials/db/settings/logs/дайджесты.
- [x] Старые 5.4.7 screenshots проверены на отсутствие секретов и удаляются как устаревшие.

## Проверка истории

- [x] История репозитория начинается с отдельного clean-source импорта, а не с локальной рабочей папки.
- [x] Все 130 достижимых коммитов `main` проверены по common-secret/user-data patterns: private keys, GitHub/OpenAI/AWS/Telegram tokens, Telegram API Hash literals, phone literals, Windows user paths и чувствительные локальные файлы.
- [x] Единственные срабатывания — явные test fixtures (`api_hash = "a" * 32` / dummy 32-hex и фиктивный телефон из одинаковых цифр); реальных credentials/session/DB/телефонов не найдено.
- [x] Старые конкретные названия публичных каналов в ранней истории не являются credentials; переписывать всю историю только ради них нецелесообразно.
- [ ] После переключения visibility проверить нативную GitHub Security/secret-scanning поверхность и при любом alert немедленно вернуть репозиторий в private до разбирательства.

## Public-facing поверхность

- [x] README описывает локальную утилиту и не обещает автоматический AI-сервис.
- [x] README/SECURITY отделяют публикацию исходников от публичного бинарного Release.
- [x] Пользовательское название и имя репозитория используют **Unofficial TelegramNewsAI** / `Unofficial-TelegramNewsAI`.
- [x] Telegram Terms / Content Licensing / Sponsored Messages указаны как внешние правила без заявления о специальном исключении.
- [x] Issue templates предупреждают не публиковать секреты и локальные данные.

## GitHub admin cleanup перед Public

- [x] Удалены устаревшие ветки; в репозитории остался только `main`.
- [x] Удалены исторические tags `v5.4.1`–`v5.4.10`.
- [x] Удалены старые GitHub Releases `v5.4.7`–`v5.4.10`.
- [x] Опубликован текущий `v5.5.0 Stable` с ZIP, SHA-256 и отдельным `TelegramNewsAI_Update.cmd`.
- [x] Описание репозитория уже нейтральное и соответствует локальной функции программы.
- [ ] Убрать вводящий в заблуждение topic `ai` (встроенного AI API нет).
- [x] Repository visibility переключена private → public.
- [x] Публичная главная страница, Actions, Releases и Issues проверены после открытия.
- [ ] GitHub Security/secret scanning продолжать наблюдать; при alert немедленно разбирать причину.

## Что НЕ требуется перед открытием исходников

- дополнительные нагрузочные тесты 60/75/100 источников;
- новый Telegram transport;
- embeddings/ML;
- сервер, Docker или облачная инфраструктура;
- новый публичный бинарный Release только ради смены visibility.

## Официальные ссылки

- https://core.telegram.org/api/terms
- https://telegram.org/tos/content-licensing
- https://core.telegram.org/api/sponsored-messages
- https://core.telegram.org/api/obtaining_api_id
