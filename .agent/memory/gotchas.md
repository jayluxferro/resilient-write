---
name: gotchas and lessons learned
description: Non-obvious things that burned time and shouldn't burn time again
type: feedback
---

# Gotchas

A running list of "things I wish someone had told me" from adjacent
work. These are not blockers, but they will make you faster if you know
them upfront.

## Tool-harness content filters

**Rule**: any text-substring that looks *remotely* like a credential can
trip a content filter that silently refuses your `Write` / `edit_file`
tool call. This is the entire reason this project exists. Specific
prefixes that will trip filters even when the rest of the token is
clearly redacted:

- `sk-ant-oat01-` / `sk-ant-api03-` (Anthropic OAT / API)
- `sk-` + 30+ alnum chars (OpenAI, anything)
- `sk-proj-` (OpenAI project key)
- `gho_`, `ghp_`, `ghu_`, `ghs_`, `ghr_` (GitHub tokens)
- `eyJ` + `.eyJ` + `.` (JWT)
- `AKIA` + 16 uppercase alnum (AWS)
- `pub` + 32 hex chars (Datadog public client key)
- `-----BEGIN ` + `PRIVATE KEY-----` (PEM)

**How to work around** (before `resilient-write` is built to do it for you):
replace the prefix too. Use `<TOKEN>` or `<REDACTED>` without the
distinguishing prefix. This loses a bit of "what kind of token" context,
but it's the difference between the write succeeding and the write
silently failing.

## `\url{}` and `\mono{}` in LaTeX moving arguments

Not directly relevant to this project but if you ever produce LaTeX
output from `resilient-write` examples: the `url` package's commands
can't be used inside `\caption{}`, `\section{}`, or anywhere that
hyperref builds a PDF bookmark. Wrap them in `\texorpdfstring{}` or avoid
them in section titles entirely.

## TikZ `\tikz@deactivatthings` on TeXLive 2022

Again not directly relevant but: some TikZ features silently fail on
TeXLive 2022 because `\tikz@check@inside@picture` is commented out in
`tikz.code.tex`. If a consumer of `resilient-write` ships LaTeX examples
and they break on old TeX distributions, the fix is a `\providecommand`
stub. See the LLM telemetry report's `macros.tex` for the pattern.

## Hugo submodules + deploy

Again tangential, but it's worth knowing because the project this was
spun out of deploys to GitHub Pages via a submodule:

- `public/` as a submodule pointing at the Pages repo is a common Hugo
  pattern.
- `git push` from the parent does NOT push submodules. You have to push
  the submodule explicitly or use a wrapper script.
- `canonifyURLs = true` + a stray `hugo server` run will bake
  `localhost:1313` URLs into `public/`, and they stay there until a
  clean wipe. Symptom: "nothing I push shows up on the live site".

## macOS `find -size`

`find -size +200k` works on macOS but `find -size +204800c` does too.
Both are fine; `+200k` is the portable one.

## macOS `find -n` vs the script's `-n` flag

If you build a CLI that takes `-n` as "dry run", make sure your argument
parsing strips it before passing the rest as a path to `find`. Otherwise
`find "-n"` fails with "illegal option" and the script dies
incomprehensibly. Ask me how I know.

## POSIX `sh` subshells and variable updates

This burned me in `compress-images.sh`:

```sh
total=0
find ... | while read f; do
  total=$((total + something))   # lost when subshell exits
done
echo "$total"  # always 0
```

Fix: write stats to a temp file inside the loop, read after. Or use
`while read` with process substitution if you're willing to require
bash. For this project's shell scripts we stay POSIX.

## Pagefind `data-pagefind-body`

For search indexing: mark `<article data-pagefind-body>` on post
content, NOT on `<main>`. If you mark the whole `main` element, the
search picks up the nav and footer too and every result's excerpt starts
with "$ cd ~/home / About / Blog / ...".

## CSS `min(px, %)` in old libsass

Hugo ships libsass 3.x which cannot evaluate `min(720px, 100%)` at
compile time and errors out with "Incompatible units". Workaround:
`width: 100%; max-width: 720px;`. Modern `dart-sass` handles it fine but
Hugo doesn't default to dart-sass on every platform.

## Hugo + hyperref + TikZ

LaTeX-specific but I'll mention it: if an h2 in a theme has
`display: flex`, any inline `<em>` or appended `<a.anchor>` in the
heading text becomes a flex item and stacks vertically. Fix:
`display: block` override on `.post-content h1..h6`.

## Git submodule status prefixes

`git status --porcelain` returns different prefixes for submodule state:
- ` m` (space, lowercase m) — submodule has dirty working tree
- ` M` (space, uppercase M) — submodule has new commits (HEAD moved)
- `MM` — both

If you filter submodule noise, match both cases.
