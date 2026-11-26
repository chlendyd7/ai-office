"""H Chat gateway client (Claude messages API).

Environment (never stored in the repo or in run records):
  H_CHAT_API_KEY      required
  H_CHAT_BASE_URL     required - gateway base (".../api/v3") or the full ".../claude/messages" endpoint
  H_CHAT_PROJECT_ID   optional - sent as X-Project-Id
  H_CHAT_AUTH_PREFIX  optional - default "Bearer ". Set it to empty if you get 401 (docs differ on this)

Request shape follows the working client in AIResume (service/app/llm/h_chat.py): no temperature,
thinking disabled, non-streaming. Only `type: text` blocks of the reply are used.
"""
from __future__ import annotations

import os
import time

import httpx

from ..core.config import LLMConfig
from .base import LLMResult

SYSTEM = "You are a helpful assistant."


class HChatError(RuntimeError):
    pass


def _block(a: dict) -> dict:
    """A PDF goes as a `document` block, a picture as an `image` block (both verified against the gateway)."""
    return {"type": "document" if a["kind"] == "pdf" else "image",
            "source": {"type": "base64", "media_type": a["media_type"], "data": a["data"]}}


class HChatClient:
    def __init__(self, cfg: LLMConfig, env=None, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        env = os.environ if env is None else env
        missing = [n for n in ("H_CHAT_API_KEY", "H_CHAT_BASE_URL") if not env.get(n)]
        if missing:
            raise ValueError(f"missing environment variable(s): {', '.join(missing)}")
        if not cfg.model or cfg.model.startswith("fake"):
            raise ValueError("llm.model must be set to a real model id for mode 'hchat'")
        self.model = cfg.model
        self._models = cfg.models
        self._max_tokens = cfg.max_tokens
        self._retries = cfg.max_retries
        self._sleep = sleep
        url = env["H_CHAT_BASE_URL"].rstrip("/")
        # accept either the gateway base (".../api/v3", as SDKs use) or the full messages endpoint
        self._url = url if url.endswith("/messages") else url + "/claude/messages"
        self._headers = {"Content-Type": "application/json",
                         "Authorization": f"{env.get('H_CHAT_AUTH_PREFIX', 'Bearer ')}{env['H_CHAT_API_KEY']}"}
        if env.get("H_CHAT_PROJECT_ID"):
            self._headers["X-Project-Id"] = env["H_CHAT_PROJECT_ID"]
        self._http = httpx.Client(timeout=cfg.timeout_s, transport=transport)

    def complete(self, *, agent: str, purpose: str, prompt: str, meta: dict, attachments: list[dict] | None = None) -> LLMResult:
        model = self._models.get(agent, self.model)
        content = prompt if not attachments else [*(_block(a) for a in attachments), {"type": "text", "text": prompt}]
        body = {
            "model": model,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self._max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        data, attempts = self._post(body)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage") or {}
        if "input_tokens" not in usage or "output_tokens" not in usage:
            raise HChatError("response has no usage (input_tokens/output_tokens): cannot meter this call")
        if not text:
            raise HChatError(f"empty text in response (stop_reason={data.get('stop_reason')!r}, usage={usage})")
        return LLMResult(text, usage["input_tokens"], usage["output_tokens"], model, attempts, data.get("model"), data.get("stop_reason"))

    def _post(self, body: dict) -> tuple[dict, int]:
        last = "unknown error"
        for attempt in range(self._retries + 1):
            if attempt:
                self._sleep(1.0 * 2 ** (attempt - 1))
            try:
                r = self._http.post(self._url, headers=self._headers, json=body)
            except httpx.TransportError as e:  # timeouts, connection errors
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {r.text[:300]!r}"
                continue
            if r.status_code >= 400:
                hint = " (set H_CHAT_AUTH_PREFIX to empty? check key/project)" if r.status_code in (401, 403) else ""
                raise HChatError(f"HTTP {r.status_code}: {r.text[:300]!r}{hint}")
            try:
                return r.json(), attempt + 1
            except ValueError:
                raise HChatError(f"non-JSON body (status {r.status_code}): {r.text[:300]!r}")
        raise HChatError(f"gave up after {self._retries + 1} attempts: {last}")
