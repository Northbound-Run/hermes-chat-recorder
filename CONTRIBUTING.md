# Contributing

Thanks for your interest in hermes-chat-recorder. This is a single, stdlib-only
Python package (`src/hermes_chat_recorder/`) that loads into a
[Hermes](https://hermes-agent.nousresearch.com) agent as a plugin and records
gateway chat traffic to an Obsidian-style Markdown vault. Issues and pull
requests are welcome.

## Development setup

```sh
git clone https://github.com/Northbound-Run/hermes-chat-recorder
cd hermes-chat-recorder
pip install -e .[dev]

pytest -q          # 300+ unit tests, no network
ruff check .       # lint
mypy src           # type-check
```

The unit tests run entirely offline — no network, no live Hermes, and no
STT/vision providers. `pytest` runs in `--import-mode=importlib` (set in
`pyproject.toml`) so the repo-root `__init__.py` (the directory-install entry
shim) doesn't interfere with test collection. **Preserve that offline
property**: a change that makes the suite require a running gateway or live
providers won't be accepted.

## Project layout & the dual-install contract

The repo supports **both** Hermes plugin-discovery paths, and the layout exists
to keep them working — don't break either:

- **`src/hermes_chat_recorder/`** is the real package (src-layout), published to
  PyPI and discovered via the `hermes_agent.plugins` entry point on the
  `pip install` path.
- **Repo-root `__init__.py`** is a thin shim for the
  `hermes plugins install Northbound-Run/hermes-chat-recorder` git-clone path: it
  puts `src/` on `sys.path` and re-exports `register`. The pip path never imports
  it. Because this path runs no `pip install`, the plugin **must stay
  stdlib-only**.
- **Two `plugin.yaml` manifests** are intentional: the repo-root copy is what the
  git-clone installer reads; the packaged copy under `src/` ships in the wheel.
  Keep them in sync.

See the README "Project layout" section and
[`HERMES_PLUGIN_STANDARD.md`](HERMES_PLUGIN_STANDARD.md) for the full rationale.

## Conventions

These mirror the README and [`docs/DESIGN.md`](docs/DESIGN.md), which are the
source of truth:

- **Stdlib-first** — no third-party runtime dependencies. STT and vision are
  delegated to Hermes's built-in `tools.transcription_tools` /
  `tools.vision_tools`, not to a bundled provider SDK. Keep it that way so the
  one-line directory install keeps working.
- **Recording-only** — the plugin archives traffic; it never decides whether the
  agent wakes. Don't add wake-gating logic here.
- **Config** — read settings from the parsed `plugins.chat_recorder` block
  (`config.py`), not scattered `os.environ.get(...)`.
- **Logging** — stdlib `logging` (`logging.getLogger(__name__)`). **Never log
  message contents, sender identifiers, or other PII**; the vault already holds
  the content, logs should not duplicate it.
- **Untrusted content** — chat message bodies are untrusted. Don't let recorded
  text steer any LLM call (e.g. the vision/STT delegation paths).
- **Offline seams** — network, Matrix client lookups, and STT/vision calls sit
  behind injectable seams so the tests stay offline. Preserve the seams.

## Pull requests

1. Fork and branch from `main`.
2. Keep changes focused; add or update tests for behavior changes.
3. Make sure `pytest -q`, `ruff check .`, and `mypy src` all pass.
4. Use [Conventional Commit](https://www.conventionalcommits.org/) subjects to
   match the history, e.g. `feat(recorder): …`, `fix(writer): …`,
   `docs(readme): …`.
5. Note any user-facing change in `CHANGELOG.md` under `## [Unreleased]`.

## Reporting bugs / security issues

Use the [issue templates](https://github.com/Northbound-Run/hermes-chat-recorder/issues/new/choose)
for bugs and feature requests. For anything security-sensitive, **do not open a
public issue** — follow [SECURITY.md](SECURITY.md) instead.
