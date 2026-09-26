# Unofficial TelegramNewsAI — Project State

Updated: 2026-09-26

## Accepted baseline

- Release: `5.5.0 Stable`
- Initial Stable acceptance commit: `c21fe3c8a7c2ac2ea9fe416f8fd5c0f1e43b89c9`
- Public `v5.5.0` release tag / verified CI artifact commit: `fc7f03c7f288172c6e2a9631a7ef53019798b1ae`
- Export schema: `8`
- Digest profile: `9.1`
- Maximum selected sources: `50`
- Production Telegram transport: Telethon / MTProto
- Web preview: research only, not production

## Accepted quality and safety state

- Real 50-source Telegram/API safety acceptance: PASS.
- No FloodWait, forced reauthorization, account/session safety signal or observed message-loss regression in the accepted Stable run.
- SQLite integrity / retention / search regression coverage: PASS.
- Event-candidate, continuity-context, direct source-link and Markdown/JSON export regressions: PASS.
- Standalone updater self-test, clean install and in-place update: PASS.
- Live updater smoke on the installed Windows copy: PASS; session, credentials, settings, selected channels and database were preserved byte-for-byte.
- Public Stable release ZIP and checksum: PASS.
- Public updater asset SHA-256: `bccb0aeb6cbc7183602b2e74c1b8fd0d58e4242a23ce4fa5ee184743be1d30d8`.
- Public quick-start PDF SHA-256: `4d494fd4ec4d5f13607c24135f8868ce2dc7d089dce20b03bc647708c9a010dc`.
- User-facing onboarding: README quick start + `docs/INSTALLATION.md` + Release PDF.
- Public repository hygiene: tracked local credentials/session/database/settings/logs are excluded; public fixtures use synthetic channel names, usernames, IDs and example links.
- Repository topic `ai` is not present; current positioning does not claim an embedded AI API.

## Current phase

**PUBLIC STABLE OPERATION / OBSERVATION**

The supported `v5.5.0 Stable` release is public and the normal product flow is complete:

**selected Telegram sources → local SQLite history → selected period/search → Markdown/JSON export → optional user-initiated external analysis.**

The application itself has no embedded AI API and performs no automatic transfer of Telegram content to an external AI/ML service.

Do not reopen Telegram/network development without a reproducible defect, recurring quality/usability problem, or safety/reliability regression. A one-off external-model mistake is not a collector/export defect when the required material is present correctly in the export.

## Source of truth

Use, in order:

1. actual repository `main`;
2. this file;
3. current coordinating chat.

README, SECURITY.md, tests and GitHub history clarify details but do not override current `main`.

## Public repository status

- Visibility: public.
- Supported Release: `v5.5.0 Stable`.
- Stable branch policy: `main` is the accepted source branch; temporary PR branches are deleted after merge.
- User-facing docs contain no real Telegram channel list or private user data.
- Regression fixtures must use synthetic channel names/usernames/IDs unless a real platform identifier is essential to the behavior being tested.
- Official Telegram/platform URLs and reserved/example URLs are allowed where they document or test protocol behavior.
- Historical one-time release/audit details remain available in Git history instead of being carried as active public documentation.

## Language handling

The public UI/documentation is Russian-first. Ukrainian text that remains inside the collector or regression suite is intentional language-processing coverage (stopwords, morphology, RU/UA aliases and multilingual Telegram-content handling), not user-specific data. Do not remove it merely for cosmetic repository cleanup if doing so would reduce search or digest quality.

## Operational guardrails

- Account safety is more important than channel count or speed.
- Keep Telegram collection sequential and stop the relevant network stage on FloodWait, PEER_FLOOD, revoked/lost session or similar safety signals.
- Do not add aggressive parallel collection, alternate scraping transports or extra Telegram APIs without demonstrated need.
- Do not repeat accepted Telegram network load tests for documentation-only or offline changes.
- API ID, API Hash, session files, credentials, tokens and passwords are secrets and must not be committed or exposed.
- Prefer small, evidence-driven fixes over broad refactors.
- If no useful next development stage exists, continue Stable operation/observation.
