# Design — hermes-chat-recorder

Reference architecture for the plugin. The deployment integration (e.g.
wiring it up for a specific bot instance) lives in the deployment's own
docs; this file is platform-neutral and operator-agnostic.

## 1. Goals

1. **Archive every Matrix message** the Hermes gateway sees — inbound
   and outbound — into per-room/per-day Markdown files in an
   Obsidian-style vault. Survive restarts, deduplicate replayed events,
   keep readable in a vanilla text editor.
2. **Make voice notes and images first-class.** Voice notes get
   transcribed via local `faster-whisper`. Images get described via an
   OpenRouter vision model. The transcript / description is what lands
   in the vault.
3. **Gate which messages wake the agent.** EITHER `@`-mention of the
   bot, OR a configured nickname (case-insensitive word match) in the
   message text. Messages that don't pass the gate are still recorded —
   they just don't trigger a reply.
4. **Stay graceful.** Per-event failures (transcription crash,
   describer outage, encrypted-media download failure) degrade silently
   to a `*_failed` marker. Startup / config / vault-write failures fail
   LOUDLY so the operator notices.

## 2. Architecture

```
Matrix room
   │
   ▼ inbound event
┌──────────────────────────────────────────┐
│ Hermes gateway/run.py:                   │
│   invokes pre_gateway_dispatch hook      │
│   (callbacks called synchronously)       │
│                                          │
│   hermes_chat_recorder.recorder          │
│     1. record placeholder section        │
│     2. if AUDIO: transcribe sync         │
│        (faster-whisper, blocking)        │
│     3. if IMAGE: describe sync           │
│        (OpenRouter HTTP, blocking)       │
│     4. update section → terminal stage   │
│     5. apply gate (@-mention or nick)    │
│     6. return skip / rewrite / allow     │
│                                          │
│   ↓ if allow / rewrite: agent dispatch   │
│   Hermes agent generates reply           │
│   Matrix adapter `send` wrapper:         │
│     record outbound reply section        │
└──────────────────────────────────────────┘
   │
   ▼ outbound event back to Matrix
```

Two integration points with Hermes:

- **Inbound:** `pre_gateway_dispatch` hook (native, in `VALID_HOOKS` at
  `hermes_cli/plugins.py`). Plugin returns `{"action": "skip" | "rewrite" | "allow"}`
  per the contract defined in `gateway/run.py`.
- **Outbound:** wrap the live Matrix adapter's `send` method during
  `on_session_start`. The wrapper is idempotent (no-ops if the adapter
  has been wrapped already) and contained within our plugin module.

**No monkey-patching of upstream Hermes source.** Both injection points
are documented public surfaces.

## 3. Vault markdown format

Per-room, per-day file at `<vault_root>/<room-slug>/<YYYY-MM-DD>.md`.
Room slug derivation:

- If the room has a canonical alias (`#name:server`), use the local
  part (`name`).
- Else use the room ID with the leading `!` stripped and `:server`
  truncated.

Day boundary follows the configured `timezone` (default
`America/Los_Angeles`). Each message uses its OWN local-date for
filename, so a reply at 00:01 lands in tomorrow's file even when the
inbound that triggered it was at 23:59 yesterday. Replies carry a
`reply_to:` field so the trail is reconstructable across day boundaries.

Section anatomy:

```markdown
<!-- event:$abcd1234:server -->
### 09:14 Annika · voice (0:12) · stage:transcribed
**audio:** mxc://server/abcdefg
**duration_sec:** 12
**mime:** audio/ogg

> okay so the deck was titled "what ai can do for your business" and
> we showed it to the meridian team last thursday

---
```

Fields:

- HTML comment `<!-- event:$id -->` is the machine-parseable idempotency
  anchor. Exact-string match — not derived from any user content — so
  no false-positive collisions with an event_id appearing inside a
  message body.
- `### <HH:MM> <sender_display_name> · <kind> · stage:<stage>` is the
  human-readable header.
- Body content varies by kind: text inline, voice as blockquote of the
  transcript, image as `description:` paragraph plus `text:` block for
  OCR'd content.
- `---` terminator separates sections.

**Stages:**

| Stage | Meaning |
|---|---|
| `received` | Event arrived; media not yet processed. |
| `transcribed` | Voice note finalized with transcript. |
| `described` | Image finalized with description + OCR text. |
| `transcribe_failed` / `describe_failed` | Terminal failure with reason field. |
| `sent` | Outbound message from the bot. |

**Idempotency contract.** When the writer processes an event, it:

1. Acquires the per-(room_slug, date) `asyncio.Lock`.
2. Reads the day file (if it exists) and scans for
   `<!-- event:$id -->`.
3. If absent → append new section in stage `received` (or `sent` for
   outbound).
4. If present and at a non-terminal stage → replace the section
   in-place with the new stage's content.
5. If present and at a terminal stage → no-op.
6. Releases the lock.

Cross-process safety is not required: only one Hermes gateway writes to
the vault path. If you run multiple gateways against the same vault,
add file-system-level locking; the package doesn't ship it.

## 4. Wake gate semantics

```python
NICKNAMES = ("ralph", "ralphy", "ralphie")  # configurable
NICKNAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in NICKNAMES) + r")\b",
    re.IGNORECASE,
)


def should_wake(*, gate_text, bot_mxid, mentioned_mxids, sender_mxid):
    if sender_mxid == bot_mxid:
        return False  # self-echo guard
    if bot_mxid in mentioned_mxids:
        return True   # @-mention always wakes, regardless of media type
    if NICKNAME_RE.search(gate_text):
        return True
    return False
```

Per message type:

| Type | `gate_text` source |
|---|---|
| Text | Message body |
| Voice | The transcribed text (or `""` if transcription failed) |
| Image | `caption + "\n" + ocr_text_block` ONLY — the freeform AI description is INTENTIONALLY excluded so model output doesn't false-wake |

**Why allowlist not regex.** A length-bounded pattern like
`\bralph[a-z]{0,3}\b` matches `ralphing`, `ralphed`, `ralpher` —
unintended. An explicit allowlist of accepted nickname forms gives
predictable behaviour and a single config knob.

## 5. Concurrency model

Hermes's `invoke_hook` (in `hermes_cli/plugins.py`) dispatches callbacks
synchronously. It does not await coroutines — an `async def` callback
returns an unawaited coroutine that the action-dict check silently
rejects.

Implication: **the `pre_gateway_dispatch` callback must be regular
`def`**, and any I/O it does (faster-whisper, OpenRouter HTTP) blocks
the gateway loop while it runs. Two patterns are possible:

- **Pattern A — sync blocking (current default).** Transcribe and
  describe inline. For low-traffic bots (one user, occasional voice
  notes) this is fine — a 30-second voice note blocks the loop for
  ~3-10s on CPU base. The package ships this.
- **Pattern B — skip-then-inject.** Hook returns `skip` immediately; an
  `asyncio.create_task` does the heavy work and re-injects a synthetic
  `MessageEvent` back into the gateway's dispatch entry. Matches the
  approach in `~/Git/northbound-os/src/mastra/channels/transcript/matrix-message-gate.ts`.
  Deferred until traffic patterns warrant it.

Switching is a config-flag swap at the recorder level; the writer / gate
/ transcriber / describer modules are pattern-agnostic.

## 6. Failure policy

Failures are classified:

| Class | Examples | Behaviour |
|---|---|---|
| Per-event soft | OpenRouter 5xx, faster-whisper crash on a malformed file, encrypted-media download fails | Log warning, write `*_failed` section, don't wake agent. Plugin continues serving other events. |
| Per-event observable | Voice note transcription failed AND the user @-mentioned the bot (configurable via `transcribe_failure_visible`) | As above PLUS post a small "transcription failed — please retry as text" reply. |
| Startup / config | Vault path not writable, bot MXID missing, OpenRouter key needed but absent at first image | Fail LOUDLY — exit non-zero, surface in gateway readiness. The plugin guarantees "Always store every message"; silently dropping that guarantee is a bug. |
| Hook plumbing | `pre_gateway_dispatch` callback exception | Hermes catches per-callback (`hermes_cli/plugins.py:1264-1298`) and logs a warning. The agent gets normal dispatch (no skip, no rewrite). Vault may be missing one event; that's preferable to crashing the gateway. |

## 7. Configuration reference

Config block under `plugins.chat_recorder` in Hermes's `config.yaml`:

```yaml
plugins:
  chat_recorder:
    enabled: true
    vault_root: /data/vault/transcripts
    nicknames: [ralph, ralphy, ralphie]
    record_outbound: true
    record_image_bytes: false
    record_audio_bytes: false
    timezone: America/Los_Angeles
    image_describer_model: google/gemini-3-flash-preview
    whisper_model_size: base
    transcribe_failure_visible: true
    pending_voice_ttl_seconds: 300  # only relevant under Pattern B
```

Env vars override the matching config keys (except `vault_root` and
`record_*`):

- `OPENROUTER_API_KEY` (required for image description)
- `IMAGE_DESCRIBER_MODEL`
- `WHISPER_MODEL_SIZE`
- `TRANSCRIPT_TZ`

The bot's MXID is read from Hermes's Matrix platform config (no
duplicate config knob).

## 8. Risk register

R1–R15 from the original plan, abbreviated:

- **R1** Plugin hook contract changes upstream → pin to a tested
  hermes-agent image digest; the plugin's `register` asserts that
  `pre_gateway_dispatch` is in `VALID_HOOKS` at load time.
- **R2** Sync-blocking transcription pauses the gateway → Pattern A
  acknowledged; switch to Pattern B if it bites.
- **R3** Append race / lifecycle bug → per-day-file `asyncio.Lock` +
  stage-based section replacement, not append-only.
- **R4** Nickname false positives → allowlist (not length-bounded
  regex), config knob, document edge cases (`"Ralph Lauren"`).
- **R5** Bot self-echo → explicit guard in `should_wake`.
- **R6** Vault disk growth → ~70-180 MB/year for a chatty room;
  revisit at 1 GB.
- **R7** Media bytes not preserved (`record_image_bytes` / `record_audio_bytes`
  default `false`) → `mxc://` is recorded; explicit doc note about
  homeserver retention.
- **R8** Markdown malformed by a stray event → writer sanitizes; every
  section ends with `---`.
- **R9** Matrix sync replay double-wakes → gate consults day file
  before deciding to wake; terminal-stage sections short-circuit.
- **R10** Encrypted media download fails → `stage:transcribe_failed`
  with `download_error: …`.
- **R11** Failure-policy classification → explicit (see §6 above).
- **R12** Crash mid-processing → placeholder written before async
  processing kicks off.
- **R13** AI description false-wakes → gate uses caption + OCR text
  ONLY, never freeform description.
- **R14** Day boundary inconsistency → own-event local-date filename;
  `reply_to:` linkage.
- **R15** Reaction events recorded as sections → filtered upfront.

## 9. Acceptance criteria

A1–A22 from the deployment plan. The package itself ships tests for
A1, A5–A6.1, A7.1, A10, A11, A16, A17, A18, A19, A22 (pure-logic and
mocked-Hermes paths). Deployment-time integration tests (A2, A3, A4,
A8, A9, A12, A13, A14, A14.1, A15, A20, A21) live in the host stack
(e.g. `paperclip-stack`).

## 10. Out of scope (today)

- Multi-Hermes-gateway scenarios writing to the same vault.
- Bridging non-Matrix platforms (the package is matrix-specific by
  intent — adding more would need per-platform message-shape
  adapters).
- Sentiment / topic indexing of transcripts (a separate downstream
  consumer's job).
- LLM-summarized digests of daily transcripts (downstream).
- Search UI (Obsidian's built-in search is the answer).
- Encrypted-at-rest storage for the vault (assumed handled by the
  underlying volume / Obsidian Sync layer).
