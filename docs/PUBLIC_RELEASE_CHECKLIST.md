# Pre-public checklist — Unofficial TelegramNewsAI

Status: **PRE-PUBLIC REPOSITORY PREPARATION**.

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
- [x] Коммиты вокруг API Hash/session hardening выборочно проверены: отслеживаемых session/credentials/DB не найдено; найденная 32-hex строка — тестовый dummy fixture.
- [x] Старые конкретные названия публичных каналов в ранней истории не являются credentials; переписывать всю историю только ради них нецелесообразно.
- [ ] Перед переключением visibility выполнить финальную GitHub secret-scanning/history check доступными средствами аккаунта.
- [ ] При необходимости удалить старые Actions runs, если в их логах когда-либо использовались реальные локальные значения.

## Public-facing поверхность

- [x] README описывает локальную утилиту и не обещает автоматический AI-сервис.
- [x] README/SECURITY отделяют публикацию исходников от публичного бинарного Release.
- [x] Пользовательское название и имя репозитория используют **Unofficial TelegramNewsAI** / `Unofficial-TelegramNewsAI`.
- [x] Telegram Terms / Content Licensing / Sponsored Messages указаны как внешние правила без заявления о специальном исключении.
- [x] Issue templates предупреждают не публиковать секреты и локальные данные.

## GitHub admin cleanup перед Public

- [ ] Удалить или осознанно оставить только нужные ветки. Устаревшие: `ci/continuity-5.5.0-validation`, `experiment/web-preview-collector`, `feature/continuity-context-5.5.0`, `fix/run3-lexical-overmerge`.
- [ ] Разобраться с историческими tags `v5.4.1`–`v5.4.10`: они не являются текущей 5.5.0 baseline.
- [ ] Удалить либо явно считать историческими старые GitHub Releases 5.4.x; они не должны выглядеть как рекомендуемая текущая сборка.
- [ ] После cleanup переключить repository visibility private → public.
- [ ] Сразу после открытия проверить публичную главную страницу, Actions, Releases, Issues и Security/secret scanning.

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
