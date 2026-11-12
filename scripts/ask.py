#!/usr/bin/env python3
"""
ask.py — the repo bridge. One command, any configured model.

    python3 scripts/ask.py doctor
    python3 scripts/ask.py models
    python3 scripts/ask.py ask "review this function" --file src/x.py
    python3 scripts/ask.py ask "grade these" --json
    python3 scripts/ask.py chat draft "outline a post"     # multi-turn memory
    python3 scripts/ask.py compare "is this sound?" --targets gemini openai:gpt-4o
    python3 scripts/ask.py map --in tasks.json             # parallel fan-out
    python3 scripts/ask.py consensus "what breaks first?" --votes 3

Design rules (do not break these):
  * stdlib only — no pip installs
  * stdout = the answer, stderr = status/logs. Never mix.
  * API keys never printed, logged, or written anywhere but the Authorization header
  * every error maps to one human sentence
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "bridge"))

import providers as P  # noqa: E402  (after sys.path tweak)

# State lives outside the repo when BRIDGE_STATE_DIR is set — this keeps test
# runs and CI from ever writing artifacts into the working tree.
STATE = Path(os.environ.get("BRIDGE_STATE_DIR") or (ROOT / ".state"))
SESSIONS = STATE / "sessions"
LOG = STATE / "calllog.jsonl"
MAX_CTX = 40_000


# ------------------------------------------------------------------- logging
def log_call(provider: str, model: str, mode: str, prompt: str, answer: str,
             usage: dict, ms: int) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "provider": provider,
           "model": model, "mode": mode, "prompt_chars": len(prompt),
           "answer_chars": len(answer), "tokens": usage, "ms": ms,
           "prompt_head": prompt[:180]}
    with LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def ok(msg: str) -> None:
    print(msg, file=sys.stderr)


def bad(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


# -------------------------------------------------------------- context files
def attach(prompt: str, files: list[str]) -> str:
    if not files:
        return prompt
    chunks = []
    for f in files:
        p = Path(f).expanduser()
        if not p.exists():
            bad(f"ERROR: --file not found: {f}")
        body = p.read_text(errors="replace")
        if len(body) > 120_000:
            body = body[:120_000] + "\n[... truncated ...]"
        chunks.append(f"===== FILE: {p.name} =====\n{body}")
    return "\n\n".join(chunks) + "\n\n===== TASK =====\n" + prompt


# ------------------------------------------------------------------ sessions
def sess_file(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-._" else "-" for c in name)[:60]
    return SESSIONS / f"{safe or 'default'}.json"


def load_session(name: str) -> dict:
    p = sess_file(name)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"name": name, "turns": []}


def save_session(s: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    sess_file(s["name"]).write_text(json.dumps(s, indent=2))


def transcript(s: dict) -> str:
    out = []
    for t in s.get("turns", []):
        out.append(f"USER: {t['q']}\nASSISTANT: {t['a']}")
    txt = "\n\n".join(out)
    return txt[-MAX_CTX:] if len(txt) > MAX_CTX else txt


# ------------------------------------------------------------------ commands
def cmd_doctor(a) -> None:
    h = P.health()
    print("Repo bridge doctor")
    print("-" * 58)
    avail = h["available"]
    if not avail:
        print("no credentials configured.\n")
        for p in h["providers"]:
            print(f"  {p['name']:<11} needs {p['env_key']}")
        print("\nAdd one line to .env, e.g.:\n  GEMINI_API_KEY=AIza...")
        sys.exit(1)
    print(f"configured: {', '.join(avail)}")
    prov = P.get(avail[0])
    print(f"smoke test: {prov.name}/{prov.default_model} ... ", end="", flush=True)
    try:
        r = prov.ask(prov.default_model, "Reply with exactly: OK", None, 0.0, 32)
        print(f"{r['text'][:40]!r} ({r['latency_ms']} ms)")
    except P.ProviderError as e:
        print(f"FAILED\n  {e}")
        sys.exit(1)
    print("-" * 58)
    print("healthy — any agent can now use this repo's bridge.")


def cmd_models(a) -> None:
    h = P.health()
    for p in h["providers"]:
        state = "ready" if p["available"] else f"needs {p['env_key']}"
        print(f"{p['name']:<11} {state:<26} default={p['default_model']}")


def resolve(a) -> tuple[str, str]:
    """figure out which provider/model to use from flag or defaults"""
    if a.provider:
        name = a.provider
        model = a.model or ""
    elif a.model and ":" in a.model:
        name, _, model = a.model.partition(":")
    elif a.model:
        name, model = "", a.model
    else:
        avail = P.available_providers()
        if not avail:
            bad("""ERROR: no provider configured.

  Fix it in 3 lines:
    cp .env.example .env && chmod 600 .env
    # then edit .env and set ONE key, e.g.
    GEMINI_API_KEY=AIza...

  Free keys:
    Gemini      https://aistudio.google.com/apikey
    OpenRouter  https://openrouter.ai/keys

  Or set the key in the process environment. Run
    python3 scripts/ask.py doctor
  to confirm.""")
        name, model = avail[0], ""
    try:
        prov = P.get(name) if name else P.get(P.available_providers()[0])
    except P.ProviderError as e:
        bad(f"ERROR: {e}")
    if not prov.available():
        bad(f"ERROR: provider '{prov.name}' has no credential. Set "
            f"{prov.env_key} in .env (see .env.example).")
    return prov.name, model or prov.default_model


HINTS = {
    "401": "the API key was rejected — check the key in .env / the Authorization header",
    "402": "out of credits — free models may still work; check the provider dashboard",
    "403": "access denied — the key may lack access to this model",
    "404": "unknown model — run `python3 scripts/ask.py models`",
    "429": "rate limited — wait, or raise the limit; retries already happened",
    "503": "provider overloaded — try again, or use a different model",
}


def explain(err: str) -> str:
    """Attach one actionable sentence to a provider error."""
    for code, hint in HINTS.items():
        if f"HTTP {code}" in err:
            return f"{err}\n  -> {hint}"
    if "network" in err.lower():
        return f"{err}\n  -> cannot reach the provider; check connectivity/proxy"
    return err


def do_ask(prov_name: str, model: str, prompt: str, system: str | None,
           temperature: float, max_tokens: int, json_mode: bool) -> dict:
    prov = P.get(prov_name)
    if json_mode:
        prompt += "\n\nRespond with a single valid JSON object and nothing else."
    return prov.ask(model, prompt, system, temperature, max_tokens)


def cmd_ask(a) -> None:
    prov_name, model = resolve(a)
    prompt = attach(a.prompt, a.file or [])
    t0 = time.time()
    try:
        r = do_ask(prov_name, model, prompt, a.system, a.temp, a.maxtok, a.json)
    except P.ProviderError as e:
        bad(f"ERROR: {prov_name}/{model}: {explain(str(e))}")
    dt = int((time.time() - t0) * 1000)
    log_call(prov_name, model, "ask", prompt, r["text"], r["usage"], dt)
    out = r["text"]
    if a.json:
        try:
            out = json.dumps(json.loads(out), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    print(out)
    if a.out:
        Path(a.out).expanduser().write_text(out)
    if not a.quiet:
        ok(f"[{prov_name}/{model} {dt} ms]")


def cmd_chat(a) -> None:
    prov_name, model = resolve(a)
    s = load_session(a.session)
    prior = transcript(s)
    prompt = attach(a.message, a.file or [])
    if prior:
        prompt = f"Conversation so far:\n\n{prior}\n\nNew message: {prompt}"
    try:
        r = do_ask(prov_name, model, prompt, a.system or None, a.temp, a.maxtok, a.json)
    except P.ProviderError as e:
        bad(f"ERROR: {prov_name}/{model}: {explain(str(e))}")
    s.setdefault("turns", []).append({"q": a.message, "a": r["text"],
                                      "model": model,
                                      "ts": datetime.now(timezone.utc).isoformat()})
    save_session(s)
    log_call(prov_name, model, f"chat:{a.session}", prompt, r["text"],
             r["usage"], r["latency_ms"])
    print(r["text"])
    if not a.quiet:
        ok(f"[{prov_name}/{model} · turn {len(s['turns'])} · {r['latency_ms']} ms]")


def _target(t: str) -> tuple[str, str]:
    name, _, model = t.partition(":")
    return name or P.available_providers()[0], model


def cmd_compare(a) -> None:
    targets = a.targets or P.available_providers()[:2]
    prompt = attach(a.prompt, a.file or [])

    def one(t: str) -> tuple[str, str]:
        name, model = _target(t)
        try:
            prov = P.get(name)
            if not prov.available():
                return t, f"[unavailable: needs {prov.env_key}]"
            r = prov.ask(model or prov.default_model, prompt, a.system,
                         a.temp, a.maxtok)
            log_call(prov.name, r["model"], "compare", prompt, r["text"],
                     r["usage"], r["latency_ms"])
            return t, r["text"]
        except P.ProviderError as e:
            return t, f"[error: {e}]"

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
        results = list(ex.map(one, targets))
    for t, ans in results:
        print(f"===== {t} =====")
        print(ans.strip())
        print()


def cmd_map(a) -> None:
    src = Path(a.infile).expanduser()
    if not src.exists():
        bad(f"ERROR: --in not found: {src}")
    items = json.loads(src.read_text())
    if not isinstance(items, list) or not items:
        bad("ERROR: --in must be a non-empty JSON array of prompt strings")
    prov_name, model = resolve(a)

    def one(p: str) -> str:
        try:
            r = do_ask(prov_name, model, str(p), a.system, a.temp, a.maxtok, False)
            log_call(prov_name, model, "map", str(p), r["text"], r["usage"],
                     r["latency_ms"])
            return r["text"]
        except P.ProviderError as e:
            return f"[error: {e}]"

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        out = list(ex.map(one, items))
    dest = Path(a.outfile or STATE / f"map-{int(time.time())}.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps([{"input": i, "output": o}
                                for i, o in zip(items, out)], indent=2))
    ok(f"{len(items)} prompt(s) in {time.time() - t0:.1f}s -> {dest}")


def cmd_consensus(a) -> None:
    """N independent answers, then a synthesis of where they agree/disagree."""
    prov_name, model = resolve(a)
    prompt = attach(a.prompt, a.file or [])
    with ThreadPoolExecutor(max_workers=a.votes) as ex:
        answers = list(ex.map(
            lambda _: do_ask(prov_name, model, prompt, a.system, 0.9, a.maxtok, False)["text"],
            range(a.votes)))
    blob = "\n\n".join(f"--- ANSWER {i + 1} ---\n{x}" for i, x in enumerate(answers))
    synth = ("Below are independent answers to the same question. Identify where they "
             "agree, where they contradict each other, and which claim is weakest. Be "
             "concise and specific.\n\nQUESTION:\n" + a.prompt + "\n\n" + blob)
    r = do_ask(prov_name, model, synth, None, 0.2, a.maxtok, False)
    print("===== SYNTHESIS =====")
    print(r["text"].strip())
    print("\n===== RAW ANSWERS =====")
    for i, x in enumerate(answers, 1):
        print(f"\n--- {i} ---\n{x.strip()}")
    log_call(prov_name, model, "consensus", a.prompt, r["text"], r["usage"],
             r["latency_ms"])


def cmd_history(a) -> None:
    s = load_session(a.session)
    if not s.get("turns"):
        print(f"(session '{a.session}' is empty)")
        return
    for t in s["turns"]:
        print(f"--- {t.get('ts', '')} [{t.get('model', '')}]")
        print(f"USER: {t['q'][:300]}")
        print(f"AI:   {t['a'][:300]}\n")


def cmd_reset(a) -> None:
    p = sess_file(a.session)
    if p.exists():
        p.unlink()
    print(f"session '{a.session}' cleared")


# ---------------------------------------------------------------------- main
def main() -> None:
    S = argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--provider", default=S)
    common.add_argument("--model", default=S, help="model id, or provider:model")
    common.add_argument("--system", default=S)
    common.add_argument("--file", action="append", default=S)
    common.add_argument("--temp", type=float, default=S)
    common.add_argument("--maxtok", type=int, default=S)
    common.add_argument("--json", action="store_true", default=S)
    common.add_argument("--out", default=S)
    common.add_argument("--quiet", action="store_true", default=S)

    ap = argparse.ArgumentParser(description="Repo bridge — ask any configured model.")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--system", default=None)
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--maxtok", type=int, default=2048)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", parents=[common]).set_defaults(fn=cmd_doctor)
    sub.add_parser("models", parents=[common]).set_defaults(fn=cmd_models)

    s = sub.add_parser("ask", parents=[common]); s.add_argument("prompt")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("chat", parents=[common]); s.add_argument("session")
    s.add_argument("message"); s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("compare", parents=[common]); s.add_argument("prompt")
    s.add_argument("--targets", nargs="+", default=None)
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("map", parents=[common]); s.add_argument("--in", dest="infile",
                                                             required=True)
    s.add_argument("--out-file", dest="outfile", default=None)
    s.add_argument("--workers", type=int, default=4); s.set_defaults(fn=cmd_map)

    s = sub.add_parser("consensus", parents=[common]); s.add_argument("prompt")
    s.add_argument("--votes", type=int, default=3); s.set_defaults(fn=cmd_consensus)

    s = sub.add_parser("history", parents=[common]); s.add_argument("session")
    s.set_defaults(fn=cmd_history)
    s = sub.add_parser("reset", parents=[common]); s.add_argument("session")
    s.set_defaults(fn=cmd_reset)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
