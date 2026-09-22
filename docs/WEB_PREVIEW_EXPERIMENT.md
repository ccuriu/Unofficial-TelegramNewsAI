# Web-preview experiment — evidence and decision

Status: **experiment completed; not accepted as production transport for the first Stable**

Experiment branch:

`experiment/web-preview-collector`

Final experiment commit:

`b3d5f6d318826e8038b7fa96387d96b44216bb36`

Production checkpoint used by the experiment:

`ed6f7c10f361e8187f0451f65259a90203e32de4`

This document intentionally carries only the final evidence and decision into `main`. The experimental collector, experiment-only requirements and experiment GitHub workflow remain outside production.

## Decision

The Telegram public Web preview is technically viable enough to remain a future transport candidate, but the experiment did **not** prove production equivalence to Telethon.

For the first Stable:

- Telethon remains the production transport;
- no Web + Telethon fallback is added;
- no experimental collector/workflow is merged;
- Web may be reconsidered only after the unexplained completeness gap is classified and the current Terms question is reviewed separately.

## Exact 24-hour A/B reference

Telethon reference interval:

`2026-09-19T14:25:56+00:00 → 2026-09-20T14:26:48+00:00`

Reference facts:

- Telethon raw message IDs in the exact interval: **798**;
- Web represented message IDs after album photo/video child recovery: **764**;
- ID-level representation: **764 / 798 = 95.7%**;
- unexplained Telethon-only IDs: **34**.

The original apparent gap was much larger before album-member recovery because Telegram Web can collapse an album into one HTML root while Telethon exposes album members as separate message objects.

The remaining 34 IDs were not fully classified as harmless album/service artifacts. Therefore the experiment cannot claim that Web loses no meaningful publication content.

## Seven-day pagination

The experiment verified practical historical pagination through `?before=<message_id>`.

Representative seven-day crawl results included:

- 1,880 HTML post roots;
- 2,206 represented Telegram message IDs before the final video-parent-link improvement;
- zero crawl errors in that run;
- high-volume sources requiring up to 45 Web pages;
- media-only publications retained.

Examples recorded by the experiment:

- `kharkivlife`: 29 pages;
- `SolovievLive`: 45 pages;
- `infantmilitario`: 29 pages.

Conclusion: seven-day `?before=` pagination is technically viable in the tested conditions.

## What Web preserved well in the experiment

The prototype could represent much of the content important for a human news digest:

- text;
- publication time;
- stable Telegram post links;
- views;
- ordinary reactions where displayed;
- external links;
- common image/video media;
- media-only posts;
- multi-item albums;
- some reply references;
- some forwarded-source references;
- an edited/not-edited flag.

## What remained weaker than MTProto

The experiment did not obtain, or did not obtain reliably:

- exact `edit_date`;
- Telegram forward count;
- comment/reply count in the tested parser;
- full reaction fidelity for every type;
- Telethon-grade media IDs/MIME/file metadata;
- native `grouped_id` semantics;
- numeric Telegram channel ID for a clean Web-only installation;
- all MTProto reply/forward metadata;
- private/non-public channels.

The key blocker before Stable is not any one secondary metadata field; it is the **34 unexplained message IDs**.

## Terms boundary

The experiment establishes technical capability only.

A public page being available without login does not by itself establish that automated scraping/aggregation is acceptable for the intended product workflow. Current Telegram API/Content Licensing terms must be assessed separately.

See:

- `docs/TELEGRAM_API_TERMS_RESEARCH_2026-09-22.md`
- https://core.telegram.org/api/terms
- https://telegram.org/tos/content-licensing

## Reconsideration gate

Reopen a production Web transport decision only if both are satisfied:

1. the 34-ID remainder is classified and no practically meaningful completeness loss remains;
2. the then-current Telegram Terms/applicability for the intended use have been reviewed.

Until then, this experiment is evidence, not production code.
