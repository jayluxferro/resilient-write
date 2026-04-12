# Install

## Runtime

```bash
# run without installing (recommended once published to PyPI)
uvx resilient-write

# or install into a pipx env
pipx install resilient-write

# or from source
git clone https://github.com/jayluxferro/resilient-write
cd resilient-write
uv sync
uv run resilient-write
```

The server speaks MCP over stdio. It takes no command-line flags — all
configuration is via environment variables (see below) and the
`.resilient_write/policy.yaml` workspace override.

## MCP client config snippets

The snippets below come in two flavours:

1. **Published** — once `resilient-write` is on PyPI, point the client at
   `uvx resilient-write`. That's what every snippet in this section
   shows first.
2. **Local checkout** — before PyPI publish (or when you want to iterate
   on the source), point the client at `uv run --directory <path>` so
   it uses the venv already synced in your working tree.

The local-checkout form for Claude Code looks like this:

```json
{
  "mcpServers": {
    "resilient-write": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/path/to/resilient-write",
        "resilient-write"
      ],
      "env": { "RW_WORKSPACE": "${PWD}" }
    }
  }
}
```

Set `--directory` to your local checkout of the repo. The same
`command` / `args` substitution applies to every client-specific
snippet below.

### Claude Code (`~/.config/claude/claude_desktop_config.json` or project `.mcp.json`)

```json
{
  "mcpServers": {
    "resilient-write": {
      "command": "uvx",
      "args": ["resilient-write"],
      "env": {
        "RW_WORKSPACE": "${PWD}"
      }
    }
  }
}
```

### Cursor (`~/.cursor/mcp.json` or project `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "resilient-write": {
      "command": "uvx",
      "args": ["resilient-write"],
      "env": {
        "RW_WORKSPACE": "${PWD}"
      }
    }
  }
}
```

### Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.resilient-write]
command = "uvx"
args = ["resilient-write"]
env = { RW_WORKSPACE = "${PWD}" }
```

### Copilot CLI (`~/.config/github-copilot/mcp.json`)

```json
{
  "mcpServers": {
    "resilient-write": {
      "command": "uvx",
      "args": ["resilient-write"],
      "env": {
        "RW_WORKSPACE": "${PWD}"
      }
    }
  }
}
```

### OpenCode (`~/.config/opencode/opencode.json` or project `opencode.json`)

```json
{
  "mcp": {
    "resilient-write": {
      "type": "local",
      "command": ["uvx", "resilient-write"],
      "environment": {
        "RW_WORKSPACE": "${PWD}"
      }
    }
  }
}
```

### Generic MCP stdio

```bash
RW_WORKSPACE=/path/to/workspace uvx resilient-write
```

Any MCP client that can spawn a stdio subprocess will work — the exact
JSON key (`mcpServers` / `mcp` / `mcp_servers`) varies per client, but
the `command`+`args`+`env` shape is the same.

## Workspace setup

On first use in a workspace, add this to `.gitignore`:

```
# resilient-write local state
.resilient_write/
HANDOFF.md
```

`rw.scratch_put` emits a non-fatal warning in its response when
`.resilient_write/` is not covered by the workspace's `.gitignore`, so
agents have a visible nudge to fix this up.

Optional: drop a policy override at `.resilient_write/policy.yaml` to
tighten or relax the L0 classifier. See [`docs/POLICY.md`](POLICY.md)
for the schema and the default pattern list.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `RW_WORKSPACE` | Workspace root (where `.resilient_write/` and `HANDOFF.md` live). | `$PWD` |
| `RW_POLICY_FILE` | Path to a custom L0 policy YAML. Absolute paths are honoured as-is; relative paths resolve against the workspace root. Missing file → fall back to defaults without error. | `.resilient_write/policy.yaml` |
| `RW_SCRATCH_DISABLE_GET` | If set to any non-empty value, every `rw.scratch_get` call returns a `policy_violation`/`permission` envelope. Use in high-sensitivity workspaces to run the scratchpad in write-only mode. | unset |

That is the full env-var surface today. Everything else is declared
inline per-call or in `.resilient_write/policy.yaml`.

## What the server exposes

| Layer | Tools |
|---|---|
| L0 | `rw.risk_score` |
| L1 | `rw.safe_write`, `rw.journal_tail` |
| L2 | `rw.chunk_write`, `rw.chunk_compose`, `rw.chunk_append`, `rw.chunk_reset`, `rw.chunk_status`, `rw.chunk_preview` |
| L4 | `rw.scratch_put`, `rw.scratch_ref`, `rw.scratch_get` |
| L5 | `rw.handoff_write`, `rw.handoff_read` |
| Utility | `rw.validate`, `rw.analytics` |

Input/output schemas: [`docs/API.md`](API.md). Failure envelopes:
[`docs/ERRORS.md`](ERRORS.md). Architecture:
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

## Verifying the install

```bash
# from a source checkout
uv run pytest
# → 186 passed

# one-shot smoke test: the server should start and exit cleanly when
# stdin is closed.
uv run resilient-write < /dev/null
```
