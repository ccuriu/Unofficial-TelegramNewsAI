# Unofficial TelegramNewsAI — Project State

Updated: 2026-09-25

## Accepted baseline

- Release: `5.5.0 Stable`
- Initial Stable acceptance commit: `c21fe3c8a7c2ac2ea9fe416f8fd5c0f1e43b89c9`
- Public `v5.5.0` release tag / verified CI artifact commit: `fc7f03c7f288172c6e2a9631a7ef53019798b1ae`
- Export schema: `8`
- Digest profile: `9.1`
- Maximum selected sources: `50`
- Production Telegram transport: Telethon / MTProto
- Web-preview: research only, not production

## Accepted gates

- Real 50-source RUN 3 network/API safety: PASS
- No FloodWait, retries, account/session safety signals or forced reauthorization in accepted RUN 3
- SQLite `PRAGMA quick_check = ok`
- Profile 8.7 event-candidate acceptance: PASS
- Profile 8.8 clickable source-link regression: PASS (CI #233, 194 tests)
- Profile 8.9 plain-chat output regression: PASS (CI #236, 195 tests)
- Profile 9.0 prompt cleanup regression: PASS (CI #240, 196 tests)
- Profile 9.1 scoped-request direct source-link regression: PASS (CI #247, 197 tests)
- Locked AI-export replacement fallback: PASS (CI #243, 197 tests)
- Portable standalone updater / no forced active-session shutdown: PASS (CI #251, 201 tests)
- Direct Stable updater without release ZIP download: PASS (PR #54; CI #274/#275, 202 tests)
- Public `v5.5.0` standalone updater asset refreshed from verified CI #275; SHA-256 `e8b77e2eed15a1fe6c6dd0b623702d766a14cb46c5e298c42f0b5a16aac28eeb`
- Stable release CI #224: 194 tests PASS
- Equal-version updater path `5.5.0 Testing -> 5.5.0 Stable`: PASS
- Local Stable installation/update: PASS
- Telegram session, credentials, settings, selected channels, database and digest history preserved
- Pre-public documentation / branding / repository hygiene: PASS (CI #259, 201 tests)
- Full pre-public Git-history secret/user-data pattern scan: PASS (130/130 commits; only explicit dummy fixtures matched)
- Pre-public accepted `main`: PASS (CI #261, 201 tests)
- Direct-updater `main` after merge: PASS (CI #275, 202 tests)
- Obsolete GitHub branches/tags/releases cleanup: PASS (one-time GitHub Actions cleanup; verified from API)
- Public `v5.5.0 Stable` Release: PASS (published from verified CI #267 artifact; ZIP checksum verified before publication)

## Current phase

**PUBLIC STABLE OPERATION / OBSERVATION**

The program baseline remains Stable. Do not reopen Telegram/network development without a reproducible defect or safety regression.

The repository is public and the supported `v5.5.0 Stable` binary Release is published. Return to normal Stable operation/observation; do not reopen Telegram/network development without a reproducible defect, recurring usability/quality problem, or safety regression.

Public source-code visibility and the `v5.5.0` binary Release are now complete. Future Releases remain separate release decisions and do not require repeating accepted network load tests unless Telegram-facing behavior changes.

## Source of truth

Use, in order:
1. actual repository `main`;
2. this file;
3. current coordinating chat.

Historical testing plans and completed specialist chats do not override current `main` / this state.

## Public repository gate

Completed before visibility changes:
- current main CI is green (CI #261, 201 tests);
- local user data / credentials are not tracked;
- obsolete public-facing screenshots were removed;
- all 130 reachable commits were scanned for common secret/user-data patterns; only explicit dummy test fixtures matched.

Completed GitHub cleanup:
- obsolete branches removed; only `main` remains;
- historical tags `v5.4.1`–`v5.4.10` removed;
- historical GitHub Releases `v5.4.7`–`v5.4.10` removed;
- repository description is already neutral and accurate.

Post-public follow-up:
- repository visibility: public;
- supported Release: `v5.5.0 Stable`;
- Branches page: only `main`;
- Releases page: only current `v5.5.0`;
- remove the repository topic `ai` if it is still present, because the application has no embedded AI API;
- keep GitHub Security/secret-scanning alerts at zero; investigate immediately if one appears.

Telegram API Terms, Content Licensing and Sponsored Messages remain external platform rules. The project documentation describes actual local behavior and does not claim a special exemption.
