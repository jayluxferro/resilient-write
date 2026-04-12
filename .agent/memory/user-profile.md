---
name: user profile
description: What the user values and how to communicate with them
type: user
---

# Working with Jay

## Who they are

- **Jay Lux Ferro** (`jay@sperixlabs.org`, `@sperixlabs`, github `jayluxferro`).
- Runs `sperixlabs.org`, a personal cybersecurity research blog.
- Focus areas: mobile reverse engineering, telemetry analysis, LLM
  tooling, privacy-preserving systems, local-first AI.
- Active maintainer of adjacent projects (`proxy-atlas` — unreleased MITM
  capture indexer; `ollama-forge` — PyPI tool for local model pipelines).
- Runs on macOS / Apple Silicon. Uses bun, Hugo, Python 3.12+.

## How they work

- **Direct and terse.** Expects the same in return. Short answers when
  short answers are possible. No preamble. No filler. No
  "I'm happy to help!" energy.
- **Wants proof, not promises.** "I built X, here's the output" beats
  "I will build X". Screenshots, greps, hashes, file listings are
  appreciated.
- **Names trade-offs explicitly.** If you're choosing X over Y, say why
  in one sentence and move on. Don't hide the decision.
- **Tracks tasks.** Uses the agent's task list heavily. Expects you to
  update it as you go.
- **Asks before risky actions.** Especially destructive git operations,
  deletes, force-pushes. Default to dry-run or preview.
- **Confirms understanding by doing the next thing.** Rarely says "ok"
  or "good"; instead asks the next question. Lack of pushback = accepted.

## Preferences that are already established

- **Code style**: no emojis unless asked, short functions, no
  speculative abstractions. Comments where non-obvious.
- **Documentation style**: structured markdown with tables for
  comparisons, code fences for examples, explicit "what this is NOT"
  sections.
- **Scripts**: POSIX `sh` for shell (not `bash`-isms unless necessary),
  `set -eu`, colour-coded status markers for long scripts.
- **Python**: 3.12+, type hints, `pyproject.toml`, prefer stdlib over
  third-party where reasonable.
- **Deploy**: one-command flows that do everything (build + commit +
  push) with `--dry-run` / `--no-push` flags for safety.

## Things to avoid

- **Repeating yourself**. If you already said "X is Y", don't say it again.
- **Summarising at the end of every response**. The user reads the diff.
- **Over-explaining technical basics**. They know what atomic rename is,
  what a stdio protocol is, what a SHA-256 is. Meet them at that level.
- **Decorative language**. "Beautifully", "elegant", "seamlessly" get
  ignored. Use them sparingly and only when they add information.
- **Unsolicited features**. Do exactly what was asked. Offer follow-up
  ideas at the end in one short list if relevant.

## What to do on a new session

1. Read `AGENT.md`.
2. Read all four files under `.agent/memory/`.
3. Check git status — if the working tree is dirty, figure out why.
4. Open by saying what you read and what you plan to do, in under 10
   lines. Wait for confirmation.

## One-liner mental model

Build the smallest useful thing first, ship it, verify it on the wire,
then ask what's next. Never build past the current stage without
confirmation.
