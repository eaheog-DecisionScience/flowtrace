"""Pluggable LLM client (OpenAI-compatible chat completions format), so
`flowtrace ask` works with any API the user provides: OpenAI itself,
Google's Gemini via its OpenAI-compatibility endpoint
(https://generativelanguage.googleapis.com/v1beta/openai/ - this is what
makes Gemini's free tier usable here without a Gemini-specific SDK),
OpenRouter, Groq, or a local model via Ollama/LM Studio.

Configuration, in order of precedence: explicit constructor args, then
env vars:
    FLOWTRACE_LLM_BASE_URL   e.g. https://api.openai.com/v1
                                   https://generativelanguage.googleapis.com/v1beta/openai
                                   http://localhost:11434/v1        (Ollama)
    FLOWTRACE_LLM_API_KEY    the provider's API key
    FLOWTRACE_LLM_MODEL      e.g. gpt-4o-mini, gemini-2.0-flash, llama3.1

Uses only the stdlib (urllib) - no extra dependency, and this sandbox has
no route to arbitrary external API hosts to test against anyway. Verified
here against a local mock OpenAI-compatible server (request shape,
Authorization header, response parsing, and HTTP error handling all
exercised for real) - NOT against a live provider. Test against your
actual endpoint before relying on this in production.
"""

import json
import os
import urllib.error
import urllib.request


class LLMConfigError(Exception):
    pass


class LLMClient:
    def __init__(self, base_url=None, api_key=None, model=None, timeout=30):
        self.base_url = (base_url or os.environ.get("FLOWTRACE_LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("FLOWTRACE_LLM_API_KEY")
        self.model = model or os.environ.get("FLOWTRACE_LLM_MODEL")
        self.timeout = timeout

        if not self.base_url:
            raise LLMConfigError(
                "No LLM endpoint configured. Pass --base-url or set FLOWTRACE_LLM_BASE_URL "
                "(e.g. https://generativelanguage.googleapis.com/v1beta/openai for Gemini's "
                "free tier, or https://api.openai.com/v1)."
            )
        if not self.model:
            raise LLMConfigError("No model configured. Pass --model or set FLOWTRACE_LLM_MODEL.")

    def chat(self, system, user, max_tokens=1024, temperature=0.2):
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request to {url} failed: HTTP {e.code} - {detail[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM request to {url} failed: {e.reason}")

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Unexpected response shape from {url}: {json.dumps(body)[:500]}") from e
