# hermes-chat-recorder

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
records every Matrix chat message — text, voice notes, images — into an
Obsidian-style markdown vault.

> **Status: alpha (pre-1.0).** APIs and config schema may change. Used in
> production at Northbound.

## What it does

| Inbound | Behaviour |
|---|---|
| Text | Append the message to `vault_root/<room>/<YYYY-MM-DD>.md`. Pass through unmodified — Hermes's normal wake settings decide whether the agent replies. |
| Voice note | Transcribe via local `faster-whisper`. Append placeholder + transcript section. Rewrite `event.text` to the transcript so the agent has usable content if it wakes. |
| Image | Describe via OpenRouter vision. Append placeholder + description section. Rewrite `event.text` to caption + description + OCR'd text. |
| Reaction | Ignored (not recorded as a message section). |
| Outbound (the agent's own reply) | Recorded with a `reply_to:` link back to the trigger. |

This plugin is **recording-only**. It does NOT decide whether the agent
wakes up — use Hermes's native `MATRIX_REQUIRE_MENTION` /
`MATRIX_ALLOWED_USERS` settings for that.

## Install

```bash
pip install hermes-chat-recorder
```

Hermes's plugin loader auto-discovers entry-point packages via the
`hermes_agent.plugins` group — no extra wiring needed. Restart the
gateway and the plugin loads (assuming it's listed in `plugins.enabled`).

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
  enabled:
    - chat_recorder
  chat_recorder:
    enabled: true
    vault_root: /data/vault/transcripts
    record_outbound: true
    timezone: America/Los_Angeles
    image_describer_model: google/gemini-3-flash-preview
    whisper_model_size: base
    prewarm_whisper: false
```

Required env vars:

- `OPENROUTER_API_KEY` — only needed if you want image description.
  Plugin degrades gracefully (records `(describer unavailable)`) if absent.

Optional env vars:

- `IMAGE_DESCRIBER_MODEL` — overrides `image_describer_model` config.
- `WHISPER_MODEL_SIZE` — overrides `whisper_model_size` (e.g. `tiny`,
  `base`, `small`, `medium`).
- `TRANSCRIPT_TZ` — overrides `timezone`.

## Markdown vault format

Per-room, per-day file at `<vault_root>/<room-slug>/<YYYY-MM-DD>.md`,
sections separated by `---`, each anchored by an HTML comment:

```markdown
<!-- event:$abcd1234:server -->
### 09:14 Annika · voice (0:12) · stage:transcribed
**mxc:** mxc://server/abcdef
**duration_sec:** 12

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
images via OpenRouter, and rewrites `event.text` to the
transcript/description; (2) a wrapped `send` on the live Matrix
adapter to capture outbound replies. Failure policy: per-event errors
are logged and written as `*_failed` stage sections;
startup/config/vault-write failures fail Hermes's readiness loudly.
See [`docs/DESIGN.md`](docs/DESIGN.md) for the full design.

## License

[MIT](LICENSE) — Copyright (c) 2026 Northbound.
