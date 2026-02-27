#!/usr/bin/env python3
"""Hybrid AI wrapper with provider switching and automatic fallback."""

from __future__ import annotations

import json
import os
import sys
import time
import base64
import hashlib
from typing import Optional

import httpx
from app.core.ai_data_policy import enforce_data_first_policy
try:
    # Force-load local .env in app runtime contexts before reading model/provider config.
    from app.core.config import app_env as _app_env_loader  # noqa: F401
except Exception:  # pragma: no cover
    _app_env_loader = None  # type: ignore[assignment]

try:
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover
    BaseModel = object  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]
try:
    import ollama
except Exception:  # pragma: no cover
    ollama = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

try:
    import anthropic
except Exception:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

try:
    from google import genai as google_genai
except Exception:  # pragma: no cover
    google_genai = None  # type: ignore[assignment]

try:
    import config
except Exception:  # pragma: no cover
    config = None  # type: ignore[assignment]


if Field is not None:
    class EnterpriseReasoningOutput(BaseModel):
        answer: str = Field(description="The primary analysis or summary.")
        confidence_score: int = Field(description="0 to 100 representing confidence in the data.")
        missing_variables: list[str] = Field(description="List of data points needed but unavailable.")
        citations: list[str] = Field(description="Exact file paths or URLs used.")
else:
    class EnterpriseReasoningOutput:  # pragma: no cover - fallback when pydantic missing
        def __init__(self, answer: str, confidence_score: int, missing_variables: list[str], citations: list[str]) -> None:
            self.answer = answer
            self.confidence_score = confidence_score
            self.missing_variables = missing_variables
            self.citations = citations


def _cfg(name: str, default: object) -> object:
    env_v = os.getenv(str(name or ""), None)
    if env_v is not None and str(env_v).strip() != "":
        return env_v
    if config is not None and hasattr(config, name):
        return getattr(config, name)
    return default


def _log(msg: str) -> None:
    if os.getenv("AI_ENGINE_SILENT", "").strip() == "1":
        return
    print(msg, file=sys.stderr)


class AIEngine:
    """
    Hybrid routing engine:
    - Cloud ("openai"/"anthropic") for complex reasoning
    - Local ("ollama") for cheap/private simple tasks

    Recommendation for financial terminals (accuracy first):
    default to cloud (`openai`) and keep local as fallback/worker.
    """

    def __init__(
        self,
        provider_override: str | None = None,
        fallback_override: str | None = None,
        model_override: str | None = None,
    ) -> None:
        self.provider = (provider_override or str(_cfg("AI_PROVIDER", "openai"))).strip().lower()
        self.fallback = str(_cfg("AI_FALLBACK_PROVIDER", "ollama")).strip().lower()
        if fallback_override:
            self.fallback = str(fallback_override).strip().lower()
        self.openai_model = str(_cfg("OPENAI_MODEL", "gpt-4o-mini")).strip()
        self.anthropic_model = str(_cfg("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")).strip()
        self.groq_model = str(_cfg("GROQ_MODEL", "llama-3.3-70b-versatile")).strip()
        self.gemini_model = str(_cfg("GEMINI_MODEL", "gemini-2.5-flash")).strip()
        self.gemini_smart_model = str(_cfg("GEMINI_SMART_MODEL", self.gemini_model or "gemini-2.5-flash")).strip()
        self.gemini_fast_model = str(_cfg("GEMINI_FAST_MODEL", "gemini-2.5-flash")).strip()
        self.gemini_fallback_models = str(
            _cfg("GEMINI_FALLBACK_MODELS", "gemini-2.0-flash,gemini-1.5-flash")
        ).strip()
        self._active_gemini_model = self.gemini_model
        self.ollama_model = str(_cfg("OLLAMA_MODEL", "llama3")).strip()
        self.ollama_smart_model = str(_cfg("OLLAMA_SMART_MODEL", "gemma2:27b")).strip()
        self.ollama_fast_model = str(_cfg("OLLAMA_FAST_MODEL", "gemma2:9b")).strip()
        if model_override:
            mo = str(model_override).strip()
            if self.provider == "openai":
                self.openai_model = mo
            elif self.provider == "groq":
                self.groq_model = mo
            elif self.provider == "anthropic":
                self.anthropic_model = mo
            elif self.provider == "ollama":
                self.ollama_model = mo
            elif self.provider == "gemini":
                self.gemini_model = mo
                self.gemini_smart_model = mo
        self.ollama_url = str(_cfg("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")).strip()
        self.timeout = float(_cfg("AI_TIMEOUT_SECONDS", 90.0))
        self.max_tokens = int(_cfg("AI_MAX_TOKENS", 3000))
        self.gemini_use_sdk = str(_cfg("GEMINI_USE_NATIVE_SDK", "0")).strip().lower() in {"1", "true", "yes", "on"}
        self.gemini_use_context_cache = str(_cfg("GEMINI_USE_CONTEXT_CACHE", "0")).strip().lower() in {"1", "true", "yes", "on"}
        self._gemini_sdk_client = None
        self._gemini_context_cache: dict[str, str] = {}
        if self.gemini_use_sdk and google_genai is not None:
            try:
                self._gemini_sdk_client = google_genai.Client()
            except Exception:
                self._gemini_sdk_client = None

    def _model_for(self, provider: str) -> str:
        p = (provider or "").strip().lower()
        if p == "openai":
            return self.openai_model
        if p == "groq":
            return self.groq_model
        if p == "gemini":
            return self._active_gemini_model or self.gemini_model
        if p == "anthropic":
            return self.anthropic_model
        if p == "ollama":
            return self.ollama_model
        return "-"

    def _gemini_model_chain(self, mode: str = "smart") -> list[str]:
        mode_key = str(mode or "").strip().lower()
        if mode_key in {"fast", "quick", "lite"}:
            first = str(self.gemini_fast_model or self.gemini_model or "").strip() or "gemini-2.5-flash"
        else:
            first = str(self.gemini_smart_model or self.gemini_model or "").strip() or "gemini-2.5-flash"
        cfg_items = [
            str(x or "").strip()
            for x in str(self.gemini_fallback_models or "").split(",")
            if str(x or "").strip()
        ]
        # If using Gemini 3 series, do not silently drop to Gemini 2.x defaults.
        first_low = first.lower()
        defaults = [] if first_low.startswith("gemini-3") else ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
        seen: set[str] = set()
        out: list[str] = []
        for model in [first] + cfg_items + defaults:
            m = str(model or "").strip()
            if not m or m in seen:
                continue
            seen.add(m)
            out.append(m)
        return out

    def _ask_openai(self, prompt: str, context: str, temperature: Optional[float] = None) -> str:
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        if OpenAI is None:
            raise RuntimeError("openai package is not available.")
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                with httpx.Client(timeout=self.timeout, trust_env=True) as client:
                    api = OpenAI(api_key=key, http_client=client)
                    resp = api.chat.completions.create(
                        model=self.openai_model,
                        messages=[
                            {"role": "system", "content": context},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=self.max_tokens,
                        temperature=(float(temperature) if temperature is not None else 0.7),
                    )
                txt = (resp.choices[0].message.content or "").strip()
                if not txt:
                    raise RuntimeError("OpenAI returned empty content.")
                return txt
            except Exception as exc:
                last_err = exc
                if attempt < 3:
                    _log(f"[AIEngine] OpenAI attempt {attempt}/3 failed; retrying...")
                    time.sleep(2 * attempt)
        raise RuntimeError(f"OpenAI failed after 3 attempts: {last_err}")

    def _ask_groq(self, prompt: str, context: str, temperature: Optional[float] = None) -> str:
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set.")
        if OpenAI is None:
            raise RuntimeError("openai package is not available.")
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                with httpx.Client(timeout=self.timeout, trust_env=True) as client:
                    api = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", http_client=client)
                    resp = api.chat.completions.create(
                        model=self.groq_model,
                        messages=[
                            {"role": "system", "content": context},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=self.max_tokens,
                        temperature=(float(temperature) if temperature is not None else 0.7),
                    )
                txt = (resp.choices[0].message.content or "").strip()
                if not txt:
                    raise RuntimeError("Groq returned empty content.")
                return txt
            except Exception as exc:
                last_err = exc
                if attempt < 3:
                    _log(f"[AIEngine] Groq attempt {attempt}/3 failed; retrying...")
                    time.sleep(2 * attempt)
        raise RuntimeError(f"Groq failed after 3 attempts: {last_err}")

    def _ask_anthropic(self, prompt: str, context: str, temperature: Optional[float] = None) -> str:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        if anthropic is None:
            raise RuntimeError("anthropic package is not available.")
        cli = anthropic.Anthropic(api_key=key)
        resp = cli.messages.create(
            model=self.anthropic_model,
            max_tokens=self.max_tokens,
            system=context,
            messages=[{"role": "user", "content": prompt}],
            temperature=(float(temperature) if temperature is not None else 0.7),
        )
        parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(str(getattr(block, "text", "")))
        txt = "".join(parts).strip()
        if not txt:
            raise RuntimeError("Anthropic returned empty content.")
        return txt

    def _ask_gemini(
        self,
        prompt: str,
        context: str,
        mode: str = "smart",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        if self.gemini_use_sdk and self._gemini_sdk_client is not None:
            try:
                return self._ask_gemini_sdk(prompt, context, mode=mode, json_mode=json_mode, temperature=temperature)
            except Exception as exc:
                _log(f"[AIEngine] Gemini native SDK failed; falling back to REST path: {exc}")
        key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set.")
        sys_txt = str(context or "").strip()
        usr_txt = str(prompt or "").strip()
        base_body = {
            "contents": [
                {"role": "user", "parts": [{"text": f"{sys_txt}\n\n{usr_txt}"}]},
            ],
            "generationConfig": {
                "temperature": float(temperature) if temperature is not None else (0.2 if json_mode else 0.7),
                "maxOutputTokens": int(self.max_tokens),
            },
        }
        if json_mode:
            base_body["generationConfig"]["responseMimeType"] = "application/json"
        errs: list[str] = []
        for model in self._gemini_model_chain(mode=mode):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            last_err: Exception | None = None
            for attempt in range(1, 3):
                try:
                    with httpx.Client(timeout=self.timeout, trust_env=True) as client:
                        resp = client.post(url, json=base_body)
                    if resp.status_code >= 400:
                        msg = ""
                        try:
                            msg = str(resp.json())
                        except Exception:
                            msg = resp.text
                        raise RuntimeError(f"HTTP {resp.status_code}: {msg[:220]}")
                    data = resp.json()
                    cands = data.get("candidates") or []
                    if not cands:
                        raise RuntimeError("Gemini returned no candidates.")
                    parts = (((cands[0] or {}).get("content") or {}).get("parts") or [])
                    text_chunks = [str((p or {}).get("text") or "") for p in parts if (p or {}).get("text")]
                    txt = "".join(text_chunks).strip()
                    if not txt:
                        raise RuntimeError("Gemini returned empty content.")
                    self._active_gemini_model = model
                    if json_mode:
                        parsed = json.loads(txt)
                        return json.dumps(parsed, ensure_ascii=False)
                    return txt
                except Exception as exc:
                    last_err = exc
                    if attempt < 2:
                        _log(f"[AIEngine] Gemini model={model} attempt {attempt}/2 failed; retrying...")
                        time.sleep(1.5 * attempt)
            errs.append(f"{model}: {last_err}")
            _log(f"[AIEngine] Gemini model {model} failed; trying next Gemini model...")
        raise RuntimeError("Gemini model chain failed: " + " | ".join(errs))

    def _ask_gemini_sdk(
        self,
        prompt: str,
        context: str,
        mode: str = "smart",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        if self._gemini_sdk_client is None:
            raise RuntimeError("Gemini native SDK client unavailable.")
        sys_txt = str(context or "").strip()
        usr_txt = str(prompt or "").strip()
        errs: list[str] = []
        for model in self._gemini_model_chain(mode=mode):
            try:
                cfg: dict[str, object] = {
                    "temperature": float(temperature) if temperature is not None else (0.2 if json_mode else 0.7),
                    "max_output_tokens": int(self.max_tokens),
                }
                if json_mode:
                    cfg["response_mime_type"] = "application/json"
                contents: object = f"{sys_txt}\n\n{usr_txt}"
                # Best-effort native context caching when SDK + API support it.
                if self.gemini_use_context_cache and sys_txt:
                    ck = hashlib.sha256(f"{model}::{sys_txt}".encode("utf-8", errors="ignore")).hexdigest()
                    cached_name = self._gemini_context_cache.get(ck, "")
                    if not cached_name:
                        try:
                            caches_api = getattr(self._gemini_sdk_client, "caches", None)
                            if caches_api is not None and hasattr(caches_api, "create"):
                                cache_obj = caches_api.create(  # type: ignore[attr-defined]
                                    model=model,
                                    config={"display_name": f"ctx-{ck[:10]}", "ttl": "3600s"},
                                    contents=[sys_txt],
                                )
                                cached_name = str(getattr(cache_obj, "name", "") or "").strip()
                                if cached_name:
                                    self._gemini_context_cache[ck] = cached_name
                        except Exception:
                            cached_name = ""
                    if cached_name:
                        # If cached-content not supported by current SDK/API, this will raise and fall back.
                        cfg["cached_content"] = cached_name
                        contents = usr_txt
                resp = self._gemini_sdk_client.models.generate_content(  # type: ignore[union-attr]
                    model=model,
                    contents=contents,
                    config=cfg,
                )
                txt = str(getattr(resp, "text", "") or "").strip()
                if not txt:
                    parts = []
                    for c in list(getattr(resp, "candidates", []) or []):
                        content = getattr(c, "content", None)
                        for p in list(getattr(content, "parts", []) or []):
                            t = getattr(p, "text", None)
                            if t:
                                parts.append(str(t))
                    txt = "".join(parts).strip()
                if not txt:
                    raise RuntimeError("Gemini SDK returned empty content.")
                self._active_gemini_model = model
                if json_mode:
                    parsed = json.loads(txt)
                    return json.dumps(parsed, ensure_ascii=False)
                return txt
            except Exception as exc:
                errs.append(f"{model}: {exc}")
                continue
        raise RuntimeError("Gemini SDK model chain failed: " + " | ".join(errs))

    def _ask_gemini_multimodal(
        self,
        prompt: str,
        context: str,
        image_bytes: bytes,
        mime_type: str = "image/png",
        mode: str = "smart",
        temperature: Optional[float] = None,
    ) -> str:
        key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set.")
        if not image_bytes:
            raise RuntimeError("image_bytes is empty.")
        safe_mime = str(mime_type or "image/png").strip().lower()
        if safe_mime not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
            safe_mime = "image/png"

        sys_txt = str(context or "").strip()
        usr_txt = str(prompt or "").strip() or "Analyze this image."
        inline_data = {
            "mimeType": safe_mime,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"{sys_txt}\n\nUser request: {usr_txt}"},
                        {"inline_data": inline_data},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": float(temperature) if temperature is not None else 0.3,
                "maxOutputTokens": int(self.max_tokens),
            },
        }
        errs: list[str] = []
        for model in self._gemini_model_chain(mode=mode):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            last_err: Exception | None = None
            for attempt in range(1, 3):
                try:
                    with httpx.Client(timeout=self.timeout, trust_env=True) as client:
                        resp = client.post(url, json=body)
                    if resp.status_code >= 400:
                        msg = ""
                        try:
                            msg = str(resp.json())
                        except Exception:
                            msg = resp.text
                        raise RuntimeError(f"HTTP {resp.status_code}: {msg[:220]}")
                    data = resp.json()
                    cands = data.get("candidates") or []
                    if not cands:
                        raise RuntimeError("Gemini returned no candidates.")
                    parts = (((cands[0] or {}).get("content") or {}).get("parts") or [])
                    txt = "".join(str((p or {}).get("text") or "") for p in parts if (p or {}).get("text")).strip()
                    if not txt:
                        raise RuntimeError("Gemini returned empty multimodal content.")
                    self._active_gemini_model = model
                    return txt
                except Exception as exc:
                    last_err = exc
                    if attempt < 2:
                        _log(f"[AIEngine] Gemini multimodal model={model} attempt {attempt}/2 failed; retrying...")
                        time.sleep(1.5 * attempt)
            errs.append(f"{model}: {last_err}")
            _log(f"[AIEngine] Gemini multimodal model {model} failed; trying next Gemini model...")
        raise RuntimeError("Gemini multimodal model chain failed: " + " | ".join(errs))

    def _ask_ollama(
        self,
        prompt: str,
        context: str,
        mode: str = "smart",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        def _ensure_valid_json(text: str) -> str:
            try:
                parsed = json.loads(text)
            except Exception as exc:
                raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from exc
            return json.dumps(parsed, ensure_ascii=False)

        def _extract_ollama_content(resp_obj: object) -> str:
            # The SDK may return a dict or a typed object depending on version.
            try:
                return str(resp_obj.get("message", {}).get("content", "")).strip()  # type: ignore[union-attr]
            except Exception:
                message = getattr(resp_obj, "message", None)
                content = getattr(message, "content", "")
                return str(content).strip()

        mode_key = str(mode).strip().lower()
        # Treat deep/smart/default as high-quality lane; only explicit fast/quick/lite uses fast lane.
        use_fast = mode_key in {"fast", "quick", "lite"}
        active_model = self.ollama_fast_model if use_fast else self.ollama_smart_model

        # Prefer official ollama client when available.
        if ollama is not None:
            kwargs: dict = {
                "model": active_model,
                "messages": [
                    {"role": "system", "content": context},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {
                    "num_ctx": 8192,
                    "temperature": float(temperature) if temperature is not None else (0.2 if json_mode else 0.7),
                },
            }
            if json_mode:
                kwargs["format"] = "json"
            resp = ollama.chat(**kwargs)
            txt = _extract_ollama_content(resp)
            if not txt:
                raise RuntimeError("Ollama returned empty content.")
            if json_mode:
                return _ensure_valid_json(txt)
            return txt

        # Fallback to HTTP API for environments without python ollama package.
        payload = {
            "model": active_model or self.ollama_model,
            "prompt": f"{context}\n\n{prompt}",
            "stream": False,
            "options": {
                "num_predict": self.max_tokens,
                "num_ctx": 8192,
                "temperature": float(temperature) if temperature is not None else (0.2 if json_mode else 0.7),
            },
        }
        if json_mode:
            payload["format"] = "json"
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            resp = client.post(self.ollama_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        txt = (data.get("response") or "").strip()
        if not txt:
            raise RuntimeError("Ollama returned empty content.")
        if json_mode:
            return _ensure_valid_json(txt)
        return txt

    def _call(
        self,
        provider: str,
        prompt: str,
        context: str,
        mode: str = "smart",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        p = (provider or "").strip().lower()
        if p == "openai":
            return self._ask_openai(prompt, context, temperature=temperature)
        if p == "groq":
            return self._ask_groq(prompt, context, temperature=temperature)
        if p == "anthropic":
            return self._ask_anthropic(prompt, context, temperature=temperature)
        if p == "gemini":
            return self._ask_gemini(prompt, context, mode=mode, json_mode=json_mode, temperature=temperature)
        if p == "ollama":
            return self._ask_ollama(prompt, context, mode=mode, json_mode=json_mode, temperature=temperature)
        raise RuntimeError(f"Unsupported AI provider: {provider}")

    def ask_ai(
        self,
        prompt: str,
        context: str,
        mode: str = "smart",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        text, _, _ = self.ask_ai_with_meta(prompt, context, mode=mode, json_mode=json_mode, temperature=temperature)
        return text

    def ask_ai_with_meta(
        self,
        prompt: str,
        context: str,
        mode: str = "smart",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> tuple[str, str, str]:
        prompt = str(prompt)
        context = str(context)
        blocked, policy_out = enforce_data_first_policy(prompt, context, json_mode=bool(json_mode))
        if blocked:
            return str(policy_out), "policy_guard", "data_first_guard"
        first = self.provider or "openai"
        second = self.fallback or ("ollama" if first != "ollama" else "openai")
        if second == first:
            second = "openai" if first != "openai" else "ollama"
        errs: list[str] = []
        for provider in (first, second):
            try:
                txt = self._call(provider, prompt, context, mode=mode, json_mode=json_mode, temperature=temperature)
                model = self._model_for(provider)
                if provider == "ollama":
                    mode_key = str(mode).strip().lower()
                    model = self.ollama_fast_model if mode_key in {"fast", "quick", "lite"} else self.ollama_smart_model
                return txt, provider, model
            except Exception as exc:
                errs.append(f"{provider}: {exc}")
                _log(f"[AIEngine] {provider} failed, trying fallback...")
        raise RuntimeError("AI request failed. " + " | ".join(errs))

    def ask_ai_vision(
        self,
        prompt: str,
        context: str,
        image_bytes: bytes,
        mime_type: str = "image/png",
        temperature: Optional[float] = None,
    ) -> str:
        # Vision is Gemini-only in this stack.
        return self._ask_gemini_multimodal(
            prompt=prompt,
            context=context,
            image_bytes=image_bytes,
            mime_type=mime_type,
            mode="smart",
            temperature=temperature,
        )

    def ask_ai_with_tools(
        self,
        prompt: str,
        context: str,
        tools: list[dict] | None = None,
        mode: str = "smart",
        temperature: Optional[float] = None,
    ) -> dict:
        """
        Native function-calling path (Gemini SDK when available).
        Returns {"text": str, "function_calls": list[dict]}.
        """
        tools = list(tools or [])
        if self.provider != "gemini" or not self.gemini_use_sdk or self._gemini_sdk_client is None:
            txt = self.ask_ai(prompt, context, mode=mode, json_mode=False, temperature=temperature)
            return {"text": txt, "function_calls": []}
        errs: list[str] = []
        for model in self._gemini_model_chain(mode=mode):
            try:
                cfg: dict[str, object] = {
                    "temperature": float(temperature) if temperature is not None else 0.3,
                    "max_output_tokens": int(self.max_tokens),
                }
                if tools:
                    cfg["tools"] = [{"function_declarations": tools}]
                resp = self._gemini_sdk_client.models.generate_content(  # type: ignore[union-attr]
                    model=model,
                    contents=[f"{str(context or '').strip()}\n\n{str(prompt or '').strip()}"],
                    config=cfg,
                )
                self._active_gemini_model = model
                text = str(getattr(resp, "text", "") or "").strip()
                calls: list[dict] = []
                for fc in list(getattr(resp, "function_calls", []) or []):
                    try:
                        calls.append(
                            {
                                "name": str(getattr(fc, "name", "") or ""),
                                "args": dict(getattr(fc, "args", {}) or {}),
                            }
                        )
                    except Exception:
                        continue
                if text or calls:
                    return {"text": text, "function_calls": calls}
                raise RuntimeError("Gemini tools path returned empty response.")
            except Exception as exc:
                errs.append(f"{model}: {exc}")
                continue
        raise RuntimeError("Gemini function-calling failed: " + " | ".join(errs))


_ENGINE = AIEngine()
_AI_TELEMETRY: dict[str, object] = {
    "calls": 0,
    "errors": 0,
    "last_error": "",
    "latency_ms_avg": 0.0,
    "latency_samples": 0,
    "provider_errors": {},
}
_CB_STATE: dict[str, float] = {"until": 0.0, "consecutive_failures": 0.0}


def get_ai_runtime_metrics() -> dict[str, object]:
    return dict(_AI_TELEMETRY)


def ask_ai(
    prompt: str,
    context: str,
    mode: str = "smart",
    json_mode: bool = False,
    temperature: Optional[float] = None,
) -> str:
    """Shared helper used by scripts that need AI text generation."""
    now = time.time()
    cb_threshold = max(2, int(float(os.getenv("AI_CB_FAIL_THRESHOLD", "3"))))
    cb_cooldown = max(5, int(float(os.getenv("AI_CB_COOLDOWN_SEC", "60"))))
    if float(_CB_STATE.get("until") or 0.0) > now:
        msg = "Service temporarily degraded. Please retry shortly."
        if json_mode:
            return json.dumps({"message": msg}, ensure_ascii=True)
        return msg
    start = time.time()
    _AI_TELEMETRY["calls"] = int(_AI_TELEMETRY.get("calls") or 0) + 1
    try:
        out = _ENGINE.ask_ai(prompt, context, mode=mode, json_mode=json_mode, temperature=temperature)
        dur = (time.time() - start) * 1000.0
        n = int(_AI_TELEMETRY.get("latency_samples") or 0)
        avg = float(_AI_TELEMETRY.get("latency_ms_avg") or 0.0)
        _AI_TELEMETRY["latency_ms_avg"] = ((avg * n) + dur) / float(n + 1)
        _AI_TELEMETRY["latency_samples"] = n + 1
        _CB_STATE["consecutive_failures"] = 0.0
        return out
    except Exception as exc:
        _AI_TELEMETRY["errors"] = int(_AI_TELEMETRY.get("errors") or 0) + 1
        _AI_TELEMETRY["last_error"] = str(exc)[:200]
        _CB_STATE["consecutive_failures"] = float(_CB_STATE.get("consecutive_failures") or 0.0) + 1.0
        if int(_CB_STATE.get("consecutive_failures") or 0.0) >= cb_threshold:
            _CB_STATE["until"] = time.time() + cb_cooldown
        msg = "Transport issue while waiting for analysis result. Please retry."
        if json_mode:
            return json.dumps({"message": msg}, ensure_ascii=True)
        return msg


def _looks_incomplete_tail(text: str) -> bool:
    t = str(text or "").rstrip()
    if len(t) < 80:
        return False
    tail = t[-1]
    # Treat normal sentence/section endings as complete.
    if tail in {".", "!", "?", ":", ";", ")", "]", "}", "`", "\"", "'"}:
        return False
    # Markdown table endings are often valid row boundaries.
    if tail == "|":
        return False
    # If we ended on plain alpha/num, this is likely cut mid-thought.
    return tail.isalnum()


def _merge_without_dup(base: str, cont: str) -> str:
    a = str(base or "")
    b = str(cont or "")
    if not b:
        return a
    # Remove repeated overlap from continuation prefix.
    max_overlap = min(len(a), len(b), 240)
    for n in range(max_overlap, 29, -1):
        if a.endswith(b[:n]):
            b = b[n:]
            break
    return (a.rstrip() + ("\n" if a and not a.endswith("\n") else "") + b.lstrip()).rstrip()


def ask_ai_complete(
    prompt: str,
    context: str,
    mode: str = "smart",
    temperature: Optional[float] = None,
    max_continuations: int = 2,
) -> str:
    """
    Generate long-form text and automatically request continuation if the
    model appears to stop mid-sentence.
    """
    out = ask_ai(prompt, context, mode=mode, json_mode=False, temperature=temperature)
    rounds = 0
    while rounds < int(max_continuations or 0) and _looks_incomplete_tail(out):
        rounds += 1
        tail = out[-1200:]
        cont_prompt = (
            "Continue the exact same answer from where it stopped.\n"
            "Do NOT repeat previous text. Output continuation only.\n\n"
            "Original task:\n"
            f"{prompt}\n\n"
            "Existing answer tail:\n"
            f"{tail}"
        )
        cont_context = str(context or "").rstrip() + "\nYou are continuing an unfinished answer."
        cont = ask_ai(cont_prompt, cont_context, mode=mode, json_mode=False, temperature=temperature)
        if not str(cont or "").strip():
            break
        out = _merge_without_dup(out, cont)
        if not _looks_incomplete_tail(cont):
            break
    return str(out or "").strip()


def ask_ai_json_schema(
    prompt: str,
    context: str,
    response_json_schema: dict,
    mode: str = "smart",
    temperature: Optional[float] = None,
) -> str:
    """
    Structured-output helper.
    Today it routes through json_mode; when Gemini native SDK schema mode is available in runtime,
    this function is the stable upgrade point.
    """
    _ = response_json_schema  # reserved for native-schema route
    return _ENGINE.ask_ai(prompt, context, mode=mode, json_mode=True, temperature=temperature)


def ask_ai_vision(
    prompt: str,
    context: str,
    image_bytes: bytes,
    mime_type: str = "image/png",
    temperature: Optional[float] = None,
) -> str:
    """Gemini multimodal helper for image + text analysis."""
    return _ENGINE.ask_ai_vision(
        prompt=prompt,
        context=context,
        image_bytes=image_bytes,
        mime_type=mime_type,
        temperature=temperature,
    )


def ask_ai_with_tools(
    prompt: str,
    context: str,
    tools: list[dict] | None = None,
    mode: str = "smart",
    temperature: Optional[float] = None,
) -> dict:
    return _ENGINE.ask_ai_with_tools(prompt, context, tools=tools, mode=mode, temperature=temperature)


def ensure_enterprise_reasoning_output(raw: object, fallback_answer: str = "") -> dict[str, object]:
    """
    Enforces epistemic-humility output contract:
    - answer
    - confidence_score (0-100)
    - missing_variables (list[str])
    - citations (list[str])
    """
    obj: dict[str, object]
    if isinstance(raw, dict):
        obj = dict(raw)
    else:
        txt = str(raw or "").strip()
        try:
            parsed = json.loads(txt) if txt else {}
            obj = parsed if isinstance(parsed, dict) else {}
        except Exception:
            obj = {}
    answer = str(obj.get("answer") or fallback_answer or "").strip()
    if not answer:
        answer = "Insufficient evidence for a confident answer yet."
    try:
        cs = int(float(obj.get("confidence_score") or 0))
    except Exception:
        cs = 0
    cs = max(0, min(100, cs))
    missing_raw = obj.get("missing_variables")
    if isinstance(missing_raw, list):
        missing = [str(x).strip() for x in missing_raw if str(x).strip()]
    else:
        missing = []
    cites_raw = obj.get("citations")
    if isinstance(cites_raw, list):
        cites = [str(x).strip() for x in cites_raw if str(x).strip()]
    else:
        cites = []

    try:
        model = EnterpriseReasoningOutput(
            answer=answer,
            confidence_score=cs,
            missing_variables=missing,
            citations=cites,
        )
        if hasattr(model, "model_dump"):
            return dict(model.model_dump())  # pydantic v2
        return dict(model.dict())  # type: ignore[attr-defined] # pydantic v1
    except Exception:
        return {
            "answer": answer,
            "confidence_score": cs,
            "missing_variables": missing,
            "citations": cites,
        }
