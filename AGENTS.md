# AGENTS.md — read this before doing anything

This repo ships a **model bridge**: a documented way for you (any AI agent) to
call a second AI model for a second opinion, cross-checks, or bulk work.

Two commands matter. Run them before you trust anything.

```bash
./scripts/bootstrap.sh          # set up / repair in one idempotent step
./scripts/verify_bridge.sh      # prove it works (offline, costs nothing)
```

`bootstrap.sh` is safe to re-run any number of times. If it prints `READY`,
the bridge is usable. If it prints `NOT READY`, fix the `FAIL` lines — do not
guess, the message tells you exactly what to do.

---

## The bridge contract

**One command. stdout is the answer, stderr is status. Never mixed.**

```bash
python3 scripts/ask.py ask "<prompt>"                        # one-shot
python3 scripts/ask.py ask "<prompt>" --file src/x.py        # attach context
python3 scripts/ask.py ask "<prompt>" --json                 # parsed JSON back
python3 scripts/ask.py ask "<prompt>" --model openrouter/meta-llama/llama-3.3-70b-instruct:free
python3 scripts/ask.py chat <session> "<msg>"                # multi-turn memory
python3 scripts/ask.py compare "<prompt>" --targets gemini openai:gpt-4o-mini
python3 scripts/ask.py map --in tasks.json --workers 4       # parallel fan-out
python3 scripts/ask.py consensus "<prompt>" --votes 3        # N answers + synthesis
python3 scripts/ask.py doctor                                # health
python3 scripts/ask.py models                                # what's configured
```

Because the answer goes to stdout, this composes:

```bash
python3 scripts/ask.py ask "summarise this" --file log.txt --quiet > summary.md
```

### When to use it

- A second opinion on a design decision before committing to it
- Code review of something you just wrote — a different model catches different bugs
- Bulk classification / extraction where you'd otherwise repeat yourself N times (`map`)
- Generating structured JSON (`--json`) for a script to consume

### When NOT to use it

- Anything you can answer yourself faster — every call costs a user's quota
- Facts that are time-sensitive: **models have stale knowledge cutoffs and will
  assert wrong facts confidently.** Verify anything dated or numeric externally.
- Secrets: never pass API keys, tokens, or credentials as prompt text

---

## Rules (non-negotiable)

1. **Never print, echo, or copy a key.** Keys live in `.env` (chmod 600) or the
   process environment. They must never appear in chat, a file you create, a
   commit, or a log line.
2. **Never edit `.env` to add a fake key to make tests pass.** If credentials are
   missing, say so.
3. **Never claim the bridge works without running `verify_bridge.sh`.** Paste its
   `RESULT:` line. "Should work" is not a result.
4. **Prefer `--quiet` inside scripts** so status lines don't pollute stdout.
5. **Budget the quota.** Free tiers are small (OpenRouter free: 20 req/min and
   ~50/day; Gemini free tier has its own limits). Use `map --workers 4`, not 20.
   A `429` means you are being too greedy — back off, don't retry in a loop.
6. **Attribute borrowed output.** If you report a model's answer, label it
   (`Gemini`, `OpenRouter/<model>`), and say when you disagree with it.
7. **stdout/stderr discipline.** Never `print()` debug output; use stderr.

---

## Known-good vs untested

State this honestly when reporting:

| Thing | Status |
|---|---|
| `verify_bridge.sh` (mock provider, all commands) | **verified** — run it, paste the RESULT line |
| Live Gemini call through the bridge | **verified** only if `doctor` says `healthy` on your machine |
| Live OpenRouter / OpenAI / Anthropic | untested until a key is configured |
| Local Ollama | untested — requires a running daemon |

If you switch the provider, re-run `bootstrap.sh` and `verify_bridge.sh` and
report what actually happened, not what should have happened.

---

## Adding another provider

One class in `bridge/providers.py`, one line in `PROVIDER_CLASSES`. Anything
speaking the OpenAI `/chat/completions` shape (OpenRouter, Groq, Together,
DeepSeek, xAI, vLLM, LM Studio) subclasses `OpenAICompatProvider` and needs
about four lines. Never hardcode a model list — call the provider's `/models`
endpoint at runtime, because rosters change.

---

## Failure playbook

| Symptom | Meaning | Fix |
|---|---|---|
| `no provider configured` | no key found | `cp .env.example .env && chmod 600 .env`, add one key |
| `HTTP 401` | key rejected | check the key value; keys page in the error hint |
| `HTTP 402` | no credits | provider dashboard; free models may still work |
| `HTTP 404` | unknown model | `python3 scripts/ask.py models` |
| `HTTP 429` | rate limited | wait; lower `--workers`; spread over time |
| `limit: 0` on a model | that model has **no free tier** | switch models; do not assume rate limiting |
| empty answer, `MAX_TOKENS` | reasoning tokens ate the budget | raise `--maxtok` above 512 |

---

## What this file is not

This is not a task list. It does not tell you what to build. It tells you what
tooling exists, what is proven, and what the rules are. Your job is whatever the
human asked for — use the bridge when it genuinely helps.
