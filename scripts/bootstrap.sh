#!/usr/bin/env bash
# bootstrap.sh — idempotent setup. Safe to run any number of times.
#
#   ./scripts/bootstrap.sh             # check + fix, ask for a key if missing
#   ./scripts/bootstrap.sh --check     # report only, exit 1 if not ready
#
# The point: ANY agent, on ANY machine, can run this and get a working bridge.
# It never overwrites an existing key and never prints a key value.
set -uo pipefail
# Never leave __pycache__ in the working tree.
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")/.." || exit 1

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1
FAILED=0
note() { printf '  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=1; }
pass() { printf '  ok    %s\n' "$*"; }

echo "repo bridge bootstrap"
echo "----------------------------------------------------------"

# 1 -- interpreter
PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  fail "no python interpreter found (need 3.9+). Install python3 and re-run."
  exit 1
fi
pyver=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
if "$PY" -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)'; then
  pass "python $pyver ($PY)"
else
  fail "python $pyver is too old; need 3.9+"
fi

# 2 -- files present
for f in bridge/providers.py scripts/ask.py; do
  [ -f "$f" ] && pass "found $f" || fail "missing $f (repo incomplete)"
done

# 3 -- credentials
KEY_SOURCE="none"
if [ -n "${GEMINI_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ] \
   || [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${OPENROUTER_API_KEY:-}" ] \
   || [ -n "${GROQ_API_KEY:-}" ]; then
  KEY_SOURCE="process env"
elif [ -f .env ] && grep -qE '^(GEMINI|OPENAI|ANTHROPIC|OPENROUTER|GROQ)_API_KEY=.+' .env 2>/dev/null; then
  KEY_SOURCE=".env"
else
  if [ -f .env ]; then
    fail "no API key configured. Edit .env and set ONE key, e.g.:
           GEMINI_API_KEY=AIza...
           free keys: https://aistudio.google.com/apikey (Gemini)
                      https://openrouter.ai/keys             (many models)"
  elif [ -f .env.example ]; then
    fail "no API key configured. Copy the template and add ONE key:
           cp .env.example .env && chmod 600 .env
           then edit .env and set e.g. GEMINI_API_KEY=AIza...
           free keys: https://aistudio.google.com/apikey (Gemini)
                      https://openrouter.ai/keys             (many models)"
  else
    fail "no API key and no .env.example to copy from"
  fi
  KEY_SOURCE="missing"
fi
[ "$KEY_SOURCE" != "missing" ] && pass "credentials source: $KEY_SOURCE"

# 4 -- .env permissions (checked whenever the file exists, keyed or not)
if [ -f .env ]; then
  perm=$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null || echo "?")
  if [ "$perm" = "600" ]; then
    pass ".env permissions 600"
  elif [ "$CHECK_ONLY" = "1" ]; then
    fail ".env is $perm, should be 600 (chmod 600 .env)"
  else
    chmod 600 .env && pass "fixed .env permissions ($perm -> 600)"
  fi
fi

# 5 -- state dir writable
if [ "$CHECK_ONLY" = "0" ]; then
  mkdir -p .state/sessions 2>/dev/null
fi
if [ -w .state ] 2>/dev/null || [ "$CHECK_ONLY" = "1" ]; then
  if [ "$CHECK_ONLY" = "0" ]; then pass ".state/ writable"; fi
else
  fail ".state/ not writable — fix: chmod u+w .state"
fi

# 6 -- stdlib import sanity
if "$PY" -c "
import sys; sys.path.insert(0,'bridge')
import providers; providers.health()
" >/dev/null 2>&1; then
  pass "provider layer imports"
else
  fail "provider layer failed to import (run: $PY -c \"import sys;sys.path.insert(0,'bridge');import providers\")"
fi

# 7 -- live smoke test (skipped in --check to stay fast/cheap)
if [ "$FAILED" = "0" ] && [ "$KEY_SOURCE" != "missing" ]; then
  if [ "$CHECK_ONLY" = "1" ]; then
    note "skipped live smoke test in --check mode"
  else
    out=$("$PY" scripts/ask.py doctor 2>&1)
    if echo "$out" | grep -q "healthy"; then
      pass "live smoke test passed"
    else
      fail "live smoke test failed:
$out"
    fi
  fi
fi

echo "----------------------------------------------------------"
if [ "$FAILED" = "0" ]; then
  echo "READY — agents can now run: python3 scripts/ask.py ask \"...\""
  echo "See AGENTS.md section 'Model bridge' for the contract."
  exit 0
else
  echo "NOT READY — fix the FAIL lines above, then re-run this script."
  exit 1
fi
