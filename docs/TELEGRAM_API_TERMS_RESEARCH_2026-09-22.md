# Telegram API / Terms research record — 2026-09-22

Status: **technical record for pre-Stable hardening; not a legal conclusion**

This document records the engineering conclusions accepted for the first Stable candidate after reviewing the current Telegram API Terms, Content Licensing / AI Scraping Terms, RPC error documentation, authorization documentation, `messages.getHistory`, the Telegram `message` constructor and Telethon documentation.

## Product limit

`MAX_SELECTED_CHANNELS = 50` is a **TelegramNewsAI product limit for the first Stable release**.

It is not an official Telegram safe limit, guarantee, quota or published threshold. Telegram does not publish a rule that 50 channels are safe and 51 are unsafe.

The supported acceptance boundary remains:

- 49 sources: supported;
- 50 sources: supported and must pass real acceptance;
- 51 sources: rejected locally before Telegram network work.

## Flood and account-level signals

### FLOOD_WAIT

Telegram documents `FLOOD_WAIT_X` as a 420 FLOOD condition requiring a wait of X seconds before repeating the action.

For TelegramNewsAI:

- `FLOOD_WAIT_X` is treated as a server rate-limit signal, not as a ban;
- `Telethon.flood_sleep_threshold = 0` keeps the first signal visible to the application;
- the first FloodWait stops the current network stage;
- TelegramNewsAI does not apply exponential retry to FloodWait.

This stop policy is deliberately more conservative than Telethon's ability to auto-sleep short waits.

### PEER_FLOOD

Telegram's current `errors.json` describes `PEER_FLOOD` as an account that is spam-reported and refers the user to `@spambot`.

Therefore TelegramNewsAI must not document `PEER_FLOOD` as the normal or expected `messages.getHistory` rate-limit for read-only collection.

### Other safety-stop errors

Account/session-level signals such as revoked/expired authorization, duplicated auth/session use, frozen-account errors and banned phone-number errors are treated as hard stops without blind retry or hidden re-authorization.

## Unofficial API clients

Telegram explicitly states that third-party API client libraries are monitored to prevent abuse, that API abuse such as flooding/spamming/fake counters can lead to bans, and that accounts logging in through unofficial API clients are automatically put under observation.

This is a reason for conservative engineering. It is **not** evidence of a published channel-count threshold and does not by itself prove that the current read-only 50-source scenario is restricted.

In the project's real tests completed before this record, ordinary use had not produced FloodWait, session loss, hidden re-authorization or an observed account restriction. This observation is useful evidence, but not a future guarantee.

## MTProto request flow

Telethon's `iter_messages` uses `messages.getHistory` for normal history iteration. Telethon documents an empirical flood-wait observation for `GetHistoryRequest` and provides `wait_time` between history requests.

TelegramNewsAI keeps history concurrency at 1.

The pre-Stable hardening intentionally does **not** replace the existing incremental cursor with a more complex combined cursor algorithm. The stable logic remains:

1. get messages newer than the locally stored cursor;
2. where useful, refresh a bounded recent window for edits and mutable metrics.

The hardening removes a redundant refresh on first sync and skips the refresh when there was no already-known message inside the configured refresh window. When a recent known message exists, the separate refresh remains because preserving edit/metrics detection and simple cursor semantics is more important than saving one logical iterator.

`history_request_wait_seconds = 0.5` remains unchanged. It controls pacing inside history iteration; it is not assumed to be a delay before every first request to a channel.

The ordinary inter-channel default is now:

`inter_channel_delay_seconds = 1.0`

This is a conservative engineering default, not an official Telegram safe interval. No ordinary-request jitter is added for anti-detection or human imitation.

## Telemetry

Each routine sync writes one local `API_SAFETY_SUMMARY`.

The application records values it can measure reliably at its own boundary, including:

- sync id;
- selected channel count;
- completed and failed channels;
- Telegram messages scanned;
- logical history iterators started;
- transient network retry count;
- FloodWait occurrence and seconds;
- safety halt and reason;
- elapsed time.

The field is deliberately named `history_iterators_started`, not `messages.getHistory request count`.

TelegramNewsAI does **not** monkeypatch Telethon or depend on private internals to guess the exact number of MTProto pages/requests. A single iterator may perform more than one API request.

Telemetry/logging must not contain API Hash, auth keys, phone codes, session contents or similar secrets.

## Entity cache and dialogs

Telethon sessions persist entity information so repeated input-entity resolution can avoid unnecessary RPC work.

TelegramNewsAI continues to use the existing local entity cache and does not turn full `get_dialogs()` into a normal every-run operation.

## Session sensitivity

A Telethon SQLite session is a sensitive credential. Telethon documents that the saved session contains enough information to log in to the Telegram account and warns not to share it.

The release/update process must continue to preserve but never publish or copy user session contents into release artifacts or logs.

## Content Licensing / AI and public release

Current Telegram API Terms section 1.5 makes API use subject to the Content Licensing and AI Scraping Terms and prohibits using, accessing or aggregating Telegram data for development, enhancement or deployment of AI/ML technologies.

Current Content Licensing terms separately prohibit scraping, indexing, harvesting, aggregation or use of Telegram data for training, fine-tuning, validation, development, enhancement, benchmarking or deployment of AI/ML, subject to the exceptions described by Telegram.

TelegramNewsAI's current architecture is narrower than an integrated AI service: it stores locally, uses SQLite FTS5/LIKE, creates JSON and does not itself send Telegram content to an AI provider. The user chooses whether to pass an exported file to an external assistant.

Those architectural facts matter, but this record does **not** conclude either:

- that TelegramNewsAI definitely violates the Terms; or
- that TelegramNewsAI definitely complies with them.

Before **public distribution**, the applicability of the current API Terms and Content Licensing / AI language to this collector → local archive → JSON → user-selected AI workflow must be reviewed separately.

This public-distribution question does not automatically block technical acceptance of a local personal Stable build.

## Sponsored Messages

Telegram API Terms section 3.3 states that an app allowing access to Telegram channel content must support official sponsored messages and must not interfere with them.

The current collector/search/export workflow does not provide a normal Telegram channel-reading UI and does not fetch the separate sponsored-message mechanism.

The applicability of section 3.3 to this exact local batch collector is therefore recorded as an **open public-release question**, not as closed and not as a categorical violation.

It must be resolved before public distribution, together with the API Terms 1.5 and Content Licensing / AI questions.

## Web preview is not an automatic compliance bypass

Public `t.me/s/...` pages can technically be fetched without a user MTProto session. That removes the direct use of the user's Telegram auth key for those HTTP requests.

It does not automatically make automated scraping/aggregation acceptable under the current Terms. Public visibility and permission for automated aggregation are separate questions.

For the first Stable, Telethon remains the production transport and there is no automatic Web fallback.

## Primary sources

Telegram:

- https://core.telegram.org/api/terms
- https://telegram.org/tos/content-licensing
- https://core.telegram.org/api/obtaining_api_id
- https://core.telegram.org/api/errors
- https://core.telegram.org/api/errors.json
- https://core.telegram.org/api/auth
- https://core.telegram.org/method/messages.getHistory
- https://core.telegram.org/constructor/message

Telethon 1.45.0 documentation:

- https://docs.telethon.dev/en/stable/modules/client.html
- https://docs.telethon.dev/en/stable/concepts/entities.html
- https://docs.telethon.dev/en/stable/modules/sessions.html

## Release decision recorded here

For the first Stable candidate:

- keep Telethon as production transport;
- keep the product maximum at 50 selected sources;
- keep sequential history access and hard-stop safety semantics;
- use 1.0 s as the default inter-channel delay;
- measure logical iterator/retry/FloodWait behavior locally;
- do not merge the experimental Web collector;
- complete real 50-source acceptance;
- handle public-distribution Terms/sponsored-message applicability as a separate release/compliance gate.
