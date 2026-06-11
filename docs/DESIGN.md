# Design — hermes-chat-recorder

Reference architecture for the plugin, current as of v0.7.x. The
deployment integration (wiring it up for a specific bot instance)
lives in the deployment's own docs; this file is operator-agnostic.

## 1. Goals

1. **Archive every chat message** the Hermes gateway sees — inbound
   and outbound, on every connected platform — into per-platform,
   per-chat, per-day Markdown files in an Obsidian-style vault.
   Survive restarts, deduplicate replayed events, stay readable in a
   vanilla text editor.
2. **Make voice notes and images first-class.** Voice notes get
   transcribed and images get described via Hermes's built-in STT and
   vision tools — whatever providers the host Hermes is configured
   for. The transcript / description is what lands in the vault, and
   it's also what the agent sees (`event.text` is rewritten).
3. **Never gate.** The recorder makes no wake decisions. Whether the
   agent replies is governed entirely by Hermes's native settings
   (mention gating, allowed users, etc.). The hook returns either
   `None` (passthrough) or `{"action": "rewrite", ...}` — never
   `skip`.
4. **Stay graceful.** Per-event failures (transcription crash, vision
   outage, missing media) degrade to a `*_failed` section marker.
   Startup / config failures fail registration LOUDLY so the operator
   notices.

## 2. Architecture

```
Any platform (Matrix, Telegram, Discord, Slack, …)
   │
   ▼ inbound event
┌────────────────────────────────────────────────┐
│ Hermes gateway/run.py:                         │
│   invokes pre_gateway_dispatch hook            │
│   (sync, before auth/pairing checks)           │
│                                                │
│   hermes_chat_recorder.recorder                │
│     0. (first message only) wire adapters:     │
│        wrap send on EVERY adapter; Matrix      │
│        also contributes client + bot MXID      │
│     1. events.extract() → EventInfo            │
│        (unified fields + Matrix enrichment)    │
│     2. record placeholder section              │
│     3. if voice: transcribe via Hermes STT     │
│     4. if image: describe via Hermes vision    │
│     5. if video/file/location: record as-is    │
│     6. update section → terminal stage         │
│     7. return None or rewrite(event.text)      │
│                                                │
│   ↓ normal Hermes dispatch (wake gating etc.)  │
│   Hermes agent generates reply                 │
│   wrapped adapter.send (all platforms):        │
│     record outbound reply section              │
└────────────────────────────────────────────────┘
   │
   ▼ outbound reply back to the platform
```

Two integration points with Hermes, both documented public surfaces —
no monkey-patching of upstream source:

- **Inbound:** the `pre_gateway_dispatch` plugin hook (in
  `hermes_cli/plugins.py` `VALID_HOOKS`; invoked from
  `gateway/run.py` with `event=`, `gateway=`, `session_store=`).
  Callbacks run synchronously; the recorder's callback accepts
  `**kwargs` for forward compatibility.
- **Outbound:** wrap each live adapter's `send` method, lazily on the
  first `pre_gateway_dispatch` (the earliest hook that receives the
  gateway object — `on_session_start` only ships a session ID). The
  wrapper is idempotent (a `_chat_recorder_send_wrapped` marker
  prevents double-wrapping) and records only successful sends
  (`SendResult.success` is honored).

### Platform-generic extraction, Matrix enrichment

Hermes dispatches a unified `MessageEvent` for every platform. The
generic path in `events.py` reads only documented unified fields:

| EventInfo field | Source |
|---|---|
| `platform` | `source.platform.value` |
| `event_id` | `event.message_id` |
| `chat_id`, `chat_type`, `chat_name` | `source.chat_id` / `chat_type` / `chat_name` |
| `sender_id`, `sender_display` | `source.user_id` / `user_name` |
| `timestamp` | `event.timestamp` (naive → tagged with system zone) |
| `kind` | `event.message_type` (TEXT/COMMAND→text, AUDIO/VOICE→voice, IMAGE/PHOTO/STICKER→image, VIDEO→video, DOCUMENT→file, LOCATION→location; anything else is skipped) |
| `body` | `event.text` |
| `media_path` | `event.media_urls[0]` — every adapter downloads (and decrypts) media to a local cache file before dispatch; remote URLs are rejected |
| `reply_to_id` | `event.reply_to_message_id` |

Matrix events additionally get an enrichment pass over the raw
mautrix event: `origin_server_ts` (authoritative server time, UTC),
`mxc://` URL + mime + audio duration (provenance metadata), a richer
sender display name, reaction flagging, and `m.replace` edit
relations. **Note:** Hermes's current Matrix adapter filters edits and
reactions before dispatch, so those two paths are dormant defense —
they keep the recorder correct if an older or future adapter passes
such events through.

### Name resolution

Folders and headers use human names, resolved per ID and cached for
the process lifetime (stable folders beat fresh names for an
archive; restart to pick up renames):

- **chat slug:** config override → cache → `chat_name` hint →
  Matrix `m.room.name` lookup → DM-peer hint (the sender's name, DMs
  only — a group never borrows its first speaker's name) → Matrix
  DM-peer lookup → ID-derived slug.
- **user display:** config override → cache → `user_name` hint →
  Matrix profile lookup → ID localpart → raw ID.

Caches are keyed `platform:id`, so a Telegram chat ID can never
collide with a Discord snowflake. Hints that look like raw IDs
(`@x:y`, `!x:y`, purely numeric) are ignored. The Matrix lookups
bridge sync→async via a dedicated background event loop
(`_background_loop.py`) because the hook runs on the gateway's loop
thread, where `asyncio.run()` would raise.

## 3. Vault markdown format

Per-platform, per-chat, per-day file at
`<vault_root>/<platform>/<chat-slug>/<YYYY-MM-DD>.md`. With
`bot_type: "1on1"` the layout flattens to
`<vault_root>/<YYYY-MM-DD>.md` (single-DM bots).

Day boundary follows the configured `timezone` (default
`America/Los_Angeles`). Each message uses its OWN local-date for the
filename, so a reply at 00:01 lands in tomorrow's file even when the
inbound that triggered it was at 23:59 yesterday. Replies carry a
`reply_to:` field so the trail is reconstructable across day
boundaries.

Section anatomy:

```markdown
<!-- event:$abcd1234:server -->
### 09:14 Annika · voice · stage:transcribed
**mxc:** mxc://server/abcdefg
**mime:** audio/ogg
**duration_sec:** 12

> okay so the deck was titled "what ai can do for your business" and
> we showed it to the meridian team last thursday

---
```

- The HTML comment `<!-- event:<id> -->` is the machine-parseable
  idempotency anchor. Exact-string match, package-controlled, never
  derived from user content — no false-positive collisions with an
  event ID appearing inside a message body.
- `### <HH:MM> <sender> · <kind> · stage:<stage>` is the
  human-readable header.
- `**key:** value` field lines carry provenance (mxc, media_path,
  mime, duration_sec, reply_to, edits).
- Body varies by kind: text inline; voice as a blockquote transcript;
  image as description plus a `**text:**` block for OCR'd content;
  video/file/location as the caption (or a placeholder).
- `---` terminates the section. Section boundaries are detected by the
  NEXT anchor, not the terminator — a body can legitimately contain a
  standalone `---` line.

**Stages:**

| Stage | Meaning |
|---|---|
| `received` | Event arrived; media not yet processed. |
| `recorded` | Terminal for kinds with no processing pipeline (video/file/location). |
| `transcribed` | Voice note finalized with transcript. |
| `described` | Image finalized with description + OCR text. |
| `transcribe_failed` / `describe_failed` | Terminal failure with reason in the body. |
| `sent` | Outbound message from the bot. |
| `edited` | A Matrix edit event (dormant — see §2). |

**Idempotency contract.** `VaultWriter.write_section`:

1. Acquires the per-(path-slug, date) `threading.Lock`.
2. Reads the day file and scans for the anchor.
3. Absent → append a new section.
4. Present at a non-terminal stage, or at a *different* terminal
   stage → replace the section in-place.
5. Present at the SAME terminal stage → no-op.

The recorder also short-circuits on `has_event()` before writing the
placeholder, so sync-replays of already-recorded events don't churn
the file. Cross-process safety is out of scope: one gateway writes to
one vault path. Run multiple gateways against the same vault only
with filesystem-level locking of your own.

## 4. Concurrency model

Hermes's `invoke_hook` dispatches callbacks synchronously and does
not await coroutines — so the `pre_gateway_dispatch` callback must be
a regular `def`, and any I/O it does blocks the gateway loop while it
runs:

- **Sync blocking (current).** Transcribe and describe inline. For
  low-traffic bots this is fine — a 30-second voice note costs a few
  seconds of gateway latency on a local Whisper `base` model, less on
  hosted STT.
- **Skip-then-inject (deferred).** Return `skip` immediately, process
  in a background task, re-inject a synthetic event. Only worth the
  complexity if traffic patterns demand it; the writer/transcriber/
  describer modules are already pattern-agnostic.

Async work the plugin *initiates* (Matrix name lookups, the legacy
mxc download fallback, sync-looking `send` wrappers that return
coroutines) is bridged through a dedicated background asyncio loop on
a daemon thread (`_background_loop.py`). `asyncio.run()` is unsafe on
the gateway's loop thread, and `run_coroutine_threadsafe` requires a
loop on a *different* thread — which is exactly what the background
loop provides.

A `threading.Lock` serializes transcription calls (provider
thread-safety varies), and per-(slug, date) locks serialize vault
writes. In flat layout all writes share one lock per date, since they
share one file.

## 5. Failure policy

| Class | Examples | Behaviour |
|---|---|---|
| Per-event soft | STT provider 5xx, vision outage, malformed audio, missing cached media | Log warning, write `*_failed` section with the reason, rewrite `event.text` to a visible failure placeholder (voice) or the caption (image). Plugin continues serving other events. |
| Startup / config | Invalid `bot_type`, malformed `name_overrides`, non-string `vault_root` | `register()` raises `ConfigError` — the plugin fails to load, loudly, rather than silently dropping its "record everything" guarantee. |
| Hook plumbing | Callback raises unexpectedly | Hermes catches per-callback and logs; dispatch proceeds normally. The vault may miss one event — preferable to crashing the gateway. |
| Upstream renames | `pre_gateway_dispatch` removed from `VALID_HOOKS` | `register()` checks `VALID_HOOKS` when running inside Hermes and raises, so the breakage is visible at startup, not as silent non-recording. |

## 6. Configuration reference

Config block under `plugins.chat_recorder` in Hermes's `config.yaml`
(see README for a commented example):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch; `false` loads the plugin inert. |
| `vault_root` | `/data/vault/transcripts` | Vault directory. |
| `record_outbound` | `true` | Record the bot's own replies. |
| `timezone` | `America/Los_Angeles` | Day-boundary zone (env `TRANSCRIPT_TZ` overrides). |
| `platforms` | `[]` (all) | Allowlist of platform names to record. |
| `bot_name` | `""` | Display name for outbound sections; default resolves via Matrix profile → `"bot"`. |
| `bot_type` | `"group"` | `"group"` = per-platform/per-chat folders; `"1on1"` = flat single-DM layout. |
| `name_overrides.rooms` / `.users` | `{}` | Manual `{raw_id: display name}` overrides; win over all lookups. |

STT (`stt:`) and vision (`auxiliary.vision:`) are host-Hermes
config, deliberately not duplicated here. Keys from pre-0.3 versions
(`whisper_model_size`, `image_describer_model`, …) are accepted
silently for back-compat but have no effect; `record_image_bytes` /
`record_audio_bytes` are rejected loudly because they were never
implemented.

## 7. Risks & mitigations

- **Upstream hook contract changes** → `register()` asserts the hook
  exists in `VALID_HOOKS`; extraction is isolated in `events.py` with
  duck-typed reads, one file to update.
- **Sync transcription pauses the gateway** → acknowledged (§4);
  switch to skip-then-inject if it bites.
- **Write races / lifecycle bugs** → per-day-file locks + stage-based
  section replacement; `has_event` holds the same lock to prevent
  read-then-append tears.
- **Sync replay double-records** → anchor scan short-circuits.
- **Crash mid-processing** → placeholder is written before any media
  processing, so the event is durable at `stage:received`.
- **Chat-ID collisions across platforms** → platform folder level +
  platform-scoped resolver caches.
- **Disk growth** → plain text; roughly 100–200 MB/year for a chatty
  multi-room deployment. Revisit at 1 GB.
- **Sensitive content** → the vault records *everything*, including
  messages from unpaired senders (the hook runs before auth). Path
  permissions and at-rest encryption are the operator's volume-layer
  responsibility.

## 8. Out of scope (today)

- Multi-gateway deployments writing to the same vault.
- Recording raw media bytes into the vault (`mxc://` URLs and cache
  paths are recorded as provenance instead).
- Sentiment / topic indexing, LLM digests, search UI — downstream
  consumers' jobs (Obsidian search covers the basics).
- Encrypted-at-rest storage (assumed handled by the volume layer).
