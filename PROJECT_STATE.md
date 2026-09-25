# Unofficial TelegramNewsAI — Project State

Updated: 2026-09-25

## Accepted baseline

- Release: `5.5.0 Stable`
- Stable release commit: `c21fe3c8a7c2ac2ea9fe416f8fd5c0f1e43b89c9`
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
- Stable release CI #224: 194 tests PASS
- Equal-version updater path `5.5.0 Testing -> 5.5.0 Stable`: PASS
- Local Stable installation/update: PASS
- Telegram session, credentials, settings, selected channels, database and digest history preserved
- Pre-public documentation / branding / repository hygiene: PASS (CI #259, 201 tests)
- Full pre-public Git-history secret/user-data pattern scan: PASS (130/130 commits; only explicit dummy fixtures matched)
- Current pre-public `main`: PASS (CI #261, 201 tests)

## Current phase

**PRE-PUBLIC REPOSITORY PREPARATION**

The program baseline remains Stable. Do not reopen Telegram/network development without a reproducible defect or safety regression.

Current work is limited to GitHub administration: remove obsolete branches/tags/releases, adjust public repository metadata, then change repository visibility. The source/documentation/history gates are already accepted.

Public source-code visibility and a new public binary Release/tag are separate decisions. Opening the source repository does not require another 50-source load test.

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

Remaining GitHub-admin steps:
- delete the four obsolete branches listed in `docs/PUBLIC_RELEASE_CHECKLIST.md`;
- delete or deliberately retain historical 5.4.x tags/releases; current decision is to remove them so they cannot look like supported builds;
- update repository description/topics for the public page;
- switch visibility private → public;
- immediately check the public Security/secret-scanning surface and the visible Releases/Branches page.

Telegram API Terms, Content Licensing and Sponsored Messages remain external platform rules. The project documentation describes actual local behavior and does not claim a special exemption.
