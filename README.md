# hermes-chat-recorder

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
records every Matrix chat message — text, voice notes, images — into an
Obsidian-style markdown vault, and optionally gates which messages wake
the agent.

> **Status: alpha (pre-1.0).** APIs and config schema may change. We're
> using it in production at Northbound; you're welcome to follow along.

## What it does

| Inbound | Behaviour |
|---|---|
| Text | Append to `vault_root/<room>/<YYYY-MM-DD>.md`. Gate decides reply. |
| Voice note | Transcribe via local `faster-whisper`. Append placeholder + transcript. Gate runs on the transcript. |
| Image | Describe via OpenRouter vision. Append placeholder + description. Gate runs on caption + OCR'd text. |
| Reaction | Ignored (not recorded as a message section). |
| Outbound (the agent's own reply) | Recorded with a `reply_to:` link back to the trigger. |

The gate accepts a message (forwards to the agent) if EITHER the bot is
`@`-mentioned in the Matrix event, OR the body contains a configured
nickname (case-insensitive word match). Otherwise the message is still
**recorded** but the agent does NOT generate a reply.

## Install

```bash
pip install hermes-chat-recorder
```

Hermes's plugin loader auto-discovers entry-point packages via the
`hermes_agent.plugins` group — no extra wiring needed. Restart the
gateway and the plugin loads.

For development against an unreleased version, install editable:

```bash
git clone https://github.com/northbound-run/hermes-chat-recorder
cd hermes-chat-recorder
pip install -e .[dev]
```

## Configure

Add to your Hermes `config.yaml`:

```yaml
plugins:
  chat_recorder:
    enabled: true
    vault_root: /data/vault/transcripts
    nicknames: [ralph, ralphy, ralphie]   # case-insensitive allowlist
    record_outbound: true
    record_image_bytes: false
    record_audio_bytes: false
    timezone: America/Los_Angeles
    image_describer_model: google/gemini-3-flash-preview
    whisper_model_size: base
    transcribe_failure_visible: true
```

Required env vars:

- `OPENROUTER_API_KEY` — only needed if you want image description.
  Plugin degrades gracefully (records `(unavailable: no key)`) if absent.

Optional env vars:

- `IMAGE_DESCRIBER_MODEL` — overrides `image_describer_model` config.
- `WHISPER_MODEL_SIZE` — overrides `whisper_model_size` (e.g. `tiny`,
  `base`, `small`, `medium`).
- `TRANSCRIPT_TZ` — overrides `timezone`.

The bot's MXID is read from your Hermes Matrix platform config — no
extra knob.

## Markdown vault format

Per-room, per-day file at `<vault_root>/<room-slug>/<YYYY-MM-DD>.md`,
sections separated by `---`, each anchored by an HTML comment:

```markdown
<!-- event:$abcd1234:server -->
### 09:14 Annika · voice (0:12) · stage:transcribed
**audio:** mxc://server/abcdef

> okay so the deck was titled "what ai can do for your business" and
> we showed it to the meridian team last thursday

---

<!-- event:$efgh5678:server -->
### 09:14 Ralph · reply · stage:sent
**reply_to:** $abcd1234:server

You bet — the AI-for-business one. I'll drop a refresher in your daily note.

---
```

Idempotency comes from the HTML-comment anchor — re-delivery of the
same Matrix `event_id` finds the existing section and either updates
its stage in-place or no-ops if already at a terminal stage.

## Architecture in one paragraph

A single Hermes plugin registers two integrations: (1) the
`pre_gateway_dispatch` hook for inbound messages, where it writes to
the vault, transcribes voice via local `faster-whisper`, describes
images via OpenRouter, and decides skip/rewrite/allow per the gate
rule; (2) a wrapped `send` on the live Matrix adapter to capture
outbound replies. Failure policy: per-event errors are logged and
written as `*_failed` stage sections; startup/config/vault-write
failures fail Hermes's readiness loudly. See [`docs/DESIGN.md`](docs/DESIGN.md)
for the full design with risk register and acceptance criteria.

## License

[MIT](LICENSE) — Copyright (c) 2026 Northbound.
