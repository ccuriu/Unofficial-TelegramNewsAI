# TelegramNewsAI — Project State

Updated: 2026-09-24

## Accepted baseline

- Release: `5.5.0 Stable`
- Stable release commit: `c21fe3c8a7c2ac2ea9fe416f8fd5c0f1e43b89c9`
- Export schema: `8`
- Digest profile: `8.9`
- Maximum selected sources: `50`
- Production Telegram transport: Telethon / MTProto
- Web-preview: research only, not production

## Accepted gates

- Real 50-source RUN 3 network/API safety: PASS
- No FloodWait, retries, account/session safety signals or forced reauthorization in accepted RUN 3
- SQLite `PRAGMA quick_check = ok`
- Profile 8.7 event-candidate acceptance: PASS
- Profile 8.8 clickable source-link regression: PASS (CI #233, 194 tests)
- Profile 8.9 plain-chat output regression: pending PR CI
- Stable release CI #224: 194 tests PASS
- Equal-version updater path `5.5.0 Testing -> 5.5.0 Stable`: PASS
- Local Stable installation/update: PASS
- Telegram session, credentials, settings, selected channels, database and digest history preserved

## Current phase

**NORMAL STABLE OPERATION / OBSERVATION**

Use the program normally. Do not create development work merely to keep the project active.

Open a new development cycle only for:
- a reproducible TelegramNewsAI defect;
- a recurring quality/usability problem visible in normal use;
- a safety or reliability regression.

Before changing code, separate:
1. TelegramNewsAI/export defect;
2. external AI-model behavior;
3. weakness or contradiction in source material;
4. acceptable editorial variation.

## Current observation gate

Evaluate 3–5 ordinary real digests, or stop earlier if a significant reproducible defect appears.

Do not:
- repeat already accepted general audits without a regression or new evidence;
- run Telegram load tests without a concrete reason;
- change code for isolated external-model errors;
- raise the 50-source product limit just for testing;
- introduce embeddings, heavy ML, a new transport, server/cloud infrastructure, or another large subsystem without demonstrated practical benefit.

## Source of truth

Use, in order:
1. actual repository `main`;
2. this file for the latest accepted project phase and baseline;
3. the current coordinating chat for temporary next-step planning.

Completed specialist chats are temporary working material and may be deleted after their result is anchored in GitHub.

## Public distribution

Public GitHub Release/tag and broader distribution are separate future decisions.

Telegram API Terms, Content Licensing / AI-related restrictions, and Sponsored Messages applicability remain separate questions to resolve before wider public distribution. They do not reopen the already accepted personal local Stable by themselves.
