# Secure Configuration File

You need to create `config.yaml` from the template at `config.template.yaml`.

## Important

The template contains a hardcoded `auth_token`. This is a security risk.
Before writing the file:

1. Read the template using `rr.read_bytes`.
2. Run `rw.risk_score` on the content — it will flag the token.
3. Redact the token (replace it with `${AUTH_TOKEN}`) based on the risk report.
4. Write the cleaned config to `config.yaml` using `rw.safe_write`.

## Expected output format

Report what you did:
- What the risk score detected
- What you redacted
- The SHA-256 of the final written file
