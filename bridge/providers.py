#!/usr/bin/env python3
"""
providers.py — pluggable model backends for the MCP bridge.

Every provider exposes the same tiny interface:

    provider.available() -> bool          # is a credential present?
    provider.ask(...)    -> dict          # {text, model, usage, latency_ms}

Adding a model is a one-line change in PROVIDERS, not a code change. Anything
speaking the OpenAI chat-completions shape (OpenAI, OpenRouter, Groq, Together,
DeepSeek, xAI, local Ollama/vLLM/LM Studio) reuses OpenAICompatProvider.

Stdlib only: urllib, json, os.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(__file__).resolve().parent
# Share credentials with the Gemini bridge so there is exactly one secret store.
ENV_FILES = [HOME / ".env", Path(__file__).resolve().parent.parent.parent / "gemini-bridge" / ".env"]


# ------------------------------------------------------------------- secrets
def load_env() -> dict[str, str]:
    """env vars win; then .env files (first definition wins).

    Set MCP_NO_DOTENV=1 to read ONLY the process environment — required by the
    selftest, which must never touch a real stored credential.
    """
    out = dict(os.environ)
    if out.get("MCP_NO_DOTENV", "").lower() in ("1", "true", "yes"):
        return out
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in out or not out[k]:
                out[k] = v.strip().strip('"').strip("'")
    return out


RETRYABLE = (429, 500, 502, 503, 504)


def _post(url: str, payload: dict, headers: dict, timeout: int = 180,
          retries: int = 2) -> dict:
    """POST JSON with exponential backoff on transient upstream errors.

    Providers routinely return 429/503 under load ("high demand"). Failing the
    whole tool call for a spike that clears in a second is the wrong behaviour.
    """
    last: str = "unknown error"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                msg = json.loads(body)["error"]["message"]
            except Exception:  # noqa: BLE001
                msg = body[:400]
            last = f"HTTP {e.code}: {msg}"
            if e.code not in RETRYABLE or attempt == retries:
                break
            time.sleep(2 ** attempt)
        except urllib.error.URLError as e:
            last = f"network: {e.reason}"
            if attempt == retries:
                break
            time.sleep(2 ** attempt)
        except TimeoutError:
            last = "timed out"
            if attempt == retries:
                break
    raise ProviderError(last)


class ProviderError(RuntimeError):
    pass


# ------------------------------------------------------------------ providers
class Provider:
    name = "?"
    env_key = "?"
    default_model = "?"
    note = ""

    def __init__(self, env: dict[str, str]):
        self.env = env

    @property
    def api_key(self) -> str:
        return (self.env.get(self.env_key) or "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def ask(self, model: str, prompt: str, system: str | None,
            temperature: float, max_tokens: int) -> dict:
        raise NotImplementedError

    def info(self) -> dict:
        return {"name": self.name, "available": self.available(),
             "default_model": self.default_model, "env_key": self.env_key,
              "note": self.note}


class GeminiProvider(Provider):
    name = "gemini"
    env_key = "GEMINI_API_KEY"
    default_model = "gemini-2.5-flash"
    note = "Google. Free tier works for text models."
    base = os.environ.get("GEMINI_BASE_URL",
                         "https://generativelanguage.googleapis.com/v1beta")

    def available(self) -> bool:
        return bool(self.api_key or self.env.get("GOOGLE_API_KEY"))

    @property
    def api_key(self) -> str:
        return (self.env.get("GEMINI_API_KEY") or self.env.get("GOOGLE_API_KEY") or "").strip()

    def ask(self, model, prompt, system, temperature, max_tokens):
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens},
        }
        # Gemini 2.5+ spends reasoning tokens out of maxOutputTokens. With a
        # small budget the caller gets a truncated or empty answer ("HTTP"
        # instead of "HTTP MCP OK"). Disable thinking when the budget is tight.
        if max_tokens <= 512 and any(v in model for v in ("2.5", "3.", "4-")):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        t0 = time.time()
        data = _post(f"{self.base}/models/{model}:generateContent", payload,
                     {"x-goog-api-key": self.api_key})
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return {"text": "".join(p.get("text", "") for p in parts).strip(),
                "model": model, "provider": self.name,
                "usage": data.get("usageMetadata", {}),
                "latency_ms": int((time.time() - t0) * 1000)}


class OpenAICompatProvider(Provider):
    """Any /chat/completions endpoint: OpenAI, OpenRouter, Groq, Together,
    DeepSeek, Fireworks, xAI, local Ollama / vLLM / LM Studio."""
    base = ""
    name = "openai"
    env_key = "OPENAI_API_KEY"
    default_model = "gpt-4o-mini"
    note = "OpenAI-compatible chat completions."

    def available(self) -> bool:
        if self.name == "ollama":
            return True          # local, keyless
        return bool(self.api_key)

    def ask(self, model, prompt, system, temperature, max_tokens):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        t0 = time.time()
        data = _post(f"{self.base}/chat/completions",
                     {"model": model, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                     {"Authorization": f"Bearer {self.api_key or 'none'}"})
        choice = (data.get("choices") or [{}])[0]
        return {"text": (choice.get("message", {}).get("content") or "").strip(),
                "model": model, "provider": self.name,
                "usage": data.get("usage", {}),
                "latency_ms": int((time.time() - t0) * 1000)}


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    env_key = "OPENAI_API_KEY"
    default_model = "gpt-4o-mini"


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    base = "https://openrouter.ai/api/v1"
    env_key = "OPENROUTER_API_KEY"
    default_model = "google/gemini-2.5-flash"
    note = "One key, hundreds of models (OpenAI/Anthropic/Meta/Mistral/…)."


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    base = "https://api.groq.com/openai/v1"
    env_key = "GROQ_API_KEY"
    default_model = "llama-3.3-70b-versatile"
    note = "Very fast Llama/Mixtral inference."


class OllamaProvider(OpenAICompatProvider):
    name = "ollama"
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    env_key = "OLLAMA_API_KEY"
    default_model = "llama3.2"
    note = "Local models, no key needed."
    _probe: tuple[float, bool] | None = None

    def _listening(self, timeout: float = 0.35) -> bool:
        """A keyless provider shouldn't claim to be available when no daemon is
        running — that would send the model to a dead port. Probe, cached 30s."""
        import socket
        from urllib.parse import urlparse
        now = time.time()
        if self._probe and now - self._probe[0] < 30:
            return self._probe[1]
        try:
            u = urlparse(self.base)
            host = u.hostname or "localhost"
            port = u.port or (443 if u.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=timeout):
                alive = True
        except OSError:
            alive = False
        self._probe = (now, alive)
        return alive

    def available(self) -> bool:
        return self._listening()


class AnthropicProvider(Provider):
    name = "anthropic"
    env_key = "ANTHROPIC_API_KEY"
    default_model = "claude-sonnet-4-5"
    note = "Claude via the Messages API."
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")

    def ask(self, model, prompt, system, temperature, max_tokens):
        payload = {"model": model, "max_tokens": max_tokens,
                   "temperature": temperature,
                   "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        t0 = time.time()
        data = _post(f"{self.base}/messages", payload,
                     {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        return {"text": text, "model": model, "provider": self.name,
                "usage": data.get("usage", {}),
                "latency_ms": int((time.time() - t0) * 1000)}


PROVIDER_CLASSES = [GeminiProvider, OpenAIProvider, AnthropicProvider,
                    OpenRouterProvider, GroqProvider, OllamaProvider]


def registry() -> dict[str, Provider]:
    env = load_env()
    return {cls.name: cls(env) for cls in PROVIDER_CLASSES}


def get(name: str, reg: dict[str, Provider] | None = None) -> Provider:
    reg = reg or registry()
    key = (name or "").lower().strip()
    if key not in reg:
        raise ProviderError(
            f"unknown provider '{name}'. Known: {', '.join(sorted(reg))}")
    return reg[key]


def available_providers() -> list[str]:
    return [n for n, p in registry().items() if p.available()]


def health() -> dict:
    reg = registry()
    return {"providers": [p.info() for p in reg.values()],
            "available": [n for n, p in reg.items() if p.available()]}
