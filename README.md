# connector

<!--
FOR AGENTS: read AGENTS.md before making changes. It defines the model bridge
contract, the non-negotiable rules, and how to verify your work.
Quick start:  ./scripts/bootstrap.sh && ./scripts/verify_bridge.sh
-->

## Model bridge (AI second opinions)

This repo can call a second AI model from any provider through one documented
command. Nothing to install — Python 3.9+ standard library only.

```bash
./scripts/bootstrap.sh                      # one-time / repair, idempotent
python3 scripts/ask.py doctor               # health check
python3 scripts/ask.py ask "review this" --file src/app.py
```

Full contract and rules: **[AGENTS.md](./AGENTS.md)**.

## Setup

```bash
cp .env.example .env && chmod 600 .env
# edit .env, set ONE key:
#   GEMINI_API_KEY=...      https://aistudio.google.com/apikey   (free tier ok)
#   OPENROUTER_API_KEY=...  https://openrouter.ai/keys           (many free models)
./scripts/bootstrap.sh
```

## Commands

| Command | Purpose |
|---|---|
| `scripts/bootstrap.sh` | set up or repair; `--check` reports only |
| `scripts/verify_bridge.sh` | offline proof it works (mock provider, no quota) |
| `scripts/ask.py ask` | one prompt → one answer |
| `scripts/ask.py chat` | multi-turn session memory |
| `scripts/ask.py compare` | same prompt across providers, side by side |
| `scripts/ask.py map` | fan-out N prompts in parallel |
| `scripts/ask.py consensus` | N answers + a synthesis of disagreements |

## Layout

```
bridge/providers.py     provider adapters (add a model in ~4 lines)
scripts/ask.py          the CLI
scripts/bootstrap.sh    idempotent setup/repair + diagnostics
scripts/verify_bridge.sh offline test suite (mock provider)
AGENTS.md               the contract agents must follow
.env.example            credential template  (.env is gitignored)
.state/                 call log + chat sessions (gitignored)
```

## Notes

- `.env` and `.state/` are gitignored. Never commit credentials.
- Free tiers are small. `429` means back off, not retry harder.
- Model knowledge has a cutoff — verify anything dated or numeric externally.
