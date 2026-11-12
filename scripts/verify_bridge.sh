#!/usr/bin/env bash
# verify_bridge.sh — proof the bridge works. Agents should run this, not assume.
#
#   ./scripts/verify_bridge.sh
#
# Uses a mock provider, so it costs nothing and needs no real key.
# Exit 0 = the bridge logic is sound. Non-zero = something is broken.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PASS=0; FAIL=0
pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
chk()  { if echo "$3" | grep -qF -- "$2"; then pass "$1"; else fail "$1  (got: ${3:0:120})"; fi; }

PY=$(command -v python3 || command -v python)
# Never read the user's real .env or hit a real API in this suite.
export MCP_NO_DOTENV=1
# Write all state to a temp dir so a verification run NEVER litters the repo.
export BRIDGE_STATE_DIR="$(mktemp -d)"
# Keep a keyless local probe from finding a real daemon and skewing --help output.
export OLLAMA_BASE_URL="http://127.0.0.1:9/v1"
export PYTHONDONTWRITEBYTECODE=1
CLEANUP_DIRS="$BRIDGE_STATE_DIR"
# Snapshot pre-existing artifacts so section 13 judges THIS run, not history.
PYC_BEFORE=$(find . -name '__pycache__' -o -name '*.pyc' 2>/dev/null | sort)
unset GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY GROQ_API_KEY GOOGLE_API_KEY

# --- mock OpenAI-compatible server ---------------------------------------
MOCK_PORT=""
for p in 8299 8300 8301 8302 8303; do
  if "$PY" -c "import socket,sys;s=socket.socket();s.settimeout(0.2);sys.exit(0 if s.connect_ex(('127.0.0.1',$p))!=0 else 1)" 2>/dev/null; then
    MOCK_PORT=$p; break
  fi
done
if [ -z "$MOCK_PORT" ]; then echo "no free port for the mock provider"; exit 1; fi

# The mock server source contains the fake key — keep it OUT of the
# state dir that section 11 audits for key leaks.
MOCK_DIR="$(mktemp -d)"
MOCK_SRC="$MOCK_DIR/_mock_bridge_srv.py"
cat > "$MOCK_SRC" <<'MOCK'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = ["This is mock answer A.", "This is mock answer B.", "This is mock answer C."]


class H(BaseHTTPRequestHandler):
    n = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(ln).decode() or "{}")
        if self.headers.get("Authorization") != "Bearer sk-mock":
            body = json.dumps({"error": {"message": "bad key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        prompt = req["messages"][-1]["content"]
        H.n += 1
        if "JSON object" in prompt:
            ans = '{"verdict":"ok","score":7}'
        elif "independent answers" in prompt:
            ans = "SYNTHESIS: they agree on the core point."
        elif "Conversation so far" in prompt:
            ans = "turn-aware reply"
        else:
            ans = BODY[(H.n - 1) % 3]
        body = json.dumps({"choices": [{"message": {"content": ans}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 6}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
MOCK

"$PY" "$MOCK_SRC" "$MOCK_PORT" &
MOCK_PID=$!
cleanup() {
  kill $MOCK_PID 2>/dev/null
  rm -rf $CLEANUP_DIRS $MOCK_DIR
}
trap cleanup EXIT

# wait for the port (max ~5s) instead of a blind sleep
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if "$PY" -c "import socket,sys;s=socket.socket();s.settimeout(0.2);sys.exit(0 if s.connect_ex(('127.0.0.1',$MOCK_PORT))==0 else 1)"; then break; fi
  sleep 0.5
done

export OPENAI_BASE_URL="http://127.0.0.1:$MOCK_PORT/v1"
export OPENAI_API_KEY=sk-mock
CLI="$PY scripts/ask.py"

echo "== 1. doctor =="
out=$($CLI doctor 2>&1); chk "doctor reports healthy" "healthy" "$out"
chk "doctor names the provider" "openai" "$out"

echo "== 2. models =="
out=$($CLI models 2>&1); chk "models lists openai ready" "openai" "$out"

echo "== 3. ask =="
out=$($CLI ask "hello there" --quiet 2>&1)
chk "ask returns text" "mock answer" "$out"

echo "== 4. json mode =="
out=$($CLI ask "grade it" --json --quiet 2>&1)
chk "json parsed and pretty-printed" '"verdict": "ok"' "$out"

echo "== 5. file attachment =="
printf 'def f():\n    return 1\n' > "$BRIDGE_STATE_DIR/_snippet.py"
out=$($CLI ask "review" --file "$BRIDGE_STATE_DIR/_snippet.py" --quiet 2>&1)
chk "ask with --file works" "mock answer" "$out"

echo "== 6. chat session memory =="
$CLI chat t1 "first thing" --quiet >/dev/null 2>&1
out=$($CLI chat t1 "second thing" --quiet 2>&1)
chk "second turn is context-aware" "turn-aware" "$out"
out=$($CLI history t1 2>&1); chk "history shows both turns" "first thing" "$out"
$CLI reset t1 >/dev/null 2>&1
out=$($CLI history t1 2>&1); chk "reset clears session" "empty" "$out"

echo "== 7. compare =="
out=$($CLI compare "same question" --targets openai 2>&1)
chk "compare returns labelled answers" "===== openai =====" "$out"

echo "== 8. map fan-out =="
echo '["a","b","c","d"]' > "$BRIDGE_STATE_DIR/_tasks.json"
out=$($CLI map --in "$BRIDGE_STATE_DIR/_tasks.json" --workers 4 2>&1)
chk "map processed all 4" "4 prompt(s)" "$out"

echo "== 9. consensus =="
out=$($CLI consensus "is this sound?" --votes 3 2>&1)
chk "consensus synthesises" "SYNTHESIS" "$out"

echo "== 10. error paths =="
out=$(OPENAI_API_KEY=sk-wrong $CLI ask "hi" 2>&1)
chk "bad key -> 401 explained" "401" "$out"
chk "bad key message names the header" "Authorization" "$out"
out=$(OPENAI_API_KEY= $CLI ask "hi" 2>&1)
chk "missing key -> points at .env" ".env" "$out"
out=$($CLI ask "hi" --file "$BRIDGE_STATE_DIR/does_not_exist" 2>&1)
chk "missing file -> clear error" "not found" "$out"

echo "== 11. key hygiene =="
if grep -rq "sk-mock" "$BRIDGE_STATE_DIR/" 2>/dev/null; then fail "key leaked into state dir"; else pass "key absent from all state/log files"; fi
[ -s "$BRIDGE_STATE_DIR/calllog.jsonl" ] && pass "call log written" || fail "call log missing"

echo "== 12. stdout/stderr separation =="
ans=$($CLI ask "hello" --quiet 2>/dev/null)
stat=$($CLI ask "hello" 2>&1 >/dev/null)
chk "answer goes to stdout only" "mock answer" "$ans"
chk "status goes to stderr only" "ms]" "$stat"

kill $MOCK_PID 2>/dev/null
trap - EXIT
rm -rf $CLEANUP_DIRS $MOCK_DIR

echo "== 13. repo stays clean (no test artifacts committed by accident) =="
# Deterministic: look for artifacts this suite could actually create, and for
# anything already tracked. Does not depend on unrelated dirty files.
PYC_AFTER=$(find . -name '__pycache__' -o -name '*.pyc' 2>/dev/null | sort)
newart=""
while IFS= read -r a; do
  [ -z "$a" ] && continue
  printf '%s\n' "$PYC_BEFORE" | grep -qxF -- "$a" || newart="$newart $a"
done <<< "$PYC_AFTER"
tracked=$(git ls-files 2>/dev/null | grep -E '__pycache__|\.pyc$|^\.state/' | head -3)
if [ -n "$newart" ] || [ -n "$tracked" ]; then
  fail "test run left artifacts:${newart} ${tracked}"
else
  pass "this run left no artifacts in the working tree"
fi

echo
echo "----------------------------------------------------------"
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
