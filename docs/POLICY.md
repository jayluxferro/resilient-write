# Default L0 classifier policy

The pattern list and thresholds below are the **shipped defaults**. Workspaces
can override or extend them via `.resilient_write/policy.yaml`.

## Pattern families

### `api_key` — generic API key shapes
| Pattern | Regex | Example match |
|---|---|---|
| Anthropic OAT | `sk-ant-oat\d+-[A-Za-z0-9_\-]{40,}` | `sk-ant-oat01-QvUvUpr4f…` |
| Anthropic API key | `sk-ant-api\d+-[A-Za-z0-9_\-]{40,}` | `sk-ant-api03-…` |
| OpenAI key | `sk-(proj-)?[A-Za-z0-9]{30,}` | `sk-proj-abc123…` |
| OpenAI project key | `sk-proj-[A-Za-z0-9_\-]{40,}` | |
| Datadog public client key | `pub[a-f0-9]{32}` | `pubea5604404508cdd…` |
| Statsig client key | `client-[A-Za-z0-9]{30,}` | `client-MkRuleRQBd6q…` |
| Azure App Insights iKey | `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` (context-sensitive) | `7d7048df-6dd0-4048-…` |
| AWS access key ID | `AKIA[0-9A-Z]{16}` | |
| Generic bearer token | `(?i)bearer\s+[A-Za-z0-9\-_\.=]{20,}` | |

### `github_pat` — GitHub tokens
| Pattern | Regex | Example match |
|---|---|---|
| `gho_` classic OAuth | `gho_[A-Za-z0-9]{36}` | `gho_61ANXfmv23C4heV…` |
| `ghp_` classic PAT | `ghp_[A-Za-z0-9]{36}` | |
| `ghu_` user token | `ghu_[A-Za-z0-9]{36}` | |
| `ghs_` server token | `ghs_[A-Za-z0-9]{36}` | |
| `ghr_` refresh token | `ghr_[A-Za-z0-9]{36}` | |

### `jwt` — JSON Web Tokens
| Pattern | Regex | Example match |
|---|---|---|
| JWT | `eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+` | Cursor session JWT |

### `pem_block` — PEM-encoded key material
| Pattern | Regex |
|---|---|
| Generic PEM | `-----BEGIN [A-Z ]+ PRIVATE KEY-----` |

### `aws_secret` — AWS secret access keys
| Pattern | Regex |
|---|---|
| 40-char base64-ish after `aws_secret_access_key` | context-sensitive |

### `pii` — personal identifiers (conservative)
| Pattern | Regex |
|---|---|
| Email | `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` |
| Phone (US/INTL) | context-sensitive |
| SSN | `\b\d{3}-\d{2}-\d{4}\b` |

### `binary_hint` — encoded / binary content
| Pattern | Detector |
|---|---|
| Large base64 block | contiguous `[A-Za-z0-9+/=]{200,}` with no whitespace |
| Protobuf dump | > 40% non-printable byte ratio on a >512-byte window |

### Size heuristics
| Check | Default threshold | Score contribution |
|---|---|---|
| `total_bytes > 100_000` | 100 KB | +0.15 |
| `total_bytes > 500_000` | 500 KB | +0.30 |
| `single_line_len > 2000` | 2000 chars | +0.20 |
| `line_count > 5000` | 5000 | +0.10 |

## Scoring

Score is a weighted sum of pattern hits plus size heuristic contributions,
normalised to `[0, 1]`.

```
pattern_weight = {
  api_key:    0.35,
  github_pat: 0.35,
  jwt:        0.25,
  pem_block:  0.50,
  aws_secret: 0.40,
  pii:        0.15,
  binary_hint:0.20,
}
```

Multiple hits from the same family are damped (score saturates at the family weight × 1.5).

Verdict thresholds:
- `high   ≥ 0.70`
- `medium ≥ 0.40`
- `low    ≥ 0.10`
- else `safe`

## Suggested actions

| Family triggered | Suggested action |
|---|---|
| `api_key`, `github_pat`, `jwt`, `pem_block`, `aws_secret` | `redact` (replace match with `{REDACTED}` or `{TOKEN_TYPE-REDACTED}`) |
| `pii` | `redact` (replace with `{PII-REDACTED}`) |
| `binary_hint` | `split` (move raw bytes to scratchpad, reference by hash) |
| Size heuristic only | `split` (switch to `chunk_compose`) |
| Long single line only | `escape` (wrap in a breakable container) |

## Workspace overrides

`.resilient_write/policy.yaml`:

```yaml
version: 1
extend_patterns:
  api_key:
    - name: internal_vendor_key
      regex: 'VN-[0-9A-F]{24}'
      weight: 0.40
disable_families: []   # e.g. ['pii'] if the workspace legitimately handles test emails
thresholds:
  high: 0.65           # tighten a bit for high-sensitivity repos
  medium: 0.35
retry_budget:
  default: 3
```

The classifier merges `extend_patterns` on top of the shipped defaults, applies
`disable_families`, and uses `thresholds` to bucket verdicts. Missing fields
fall back to the defaults above.
