#!/usr/bin/env python3
from __future__ import annotations

import os
import random
import time

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

try:
    import ollama
except Exception:
    ollama = None


def _build_prompt(daily_text: str, weekly_text: str, monthly_text: str, quarterly_text: str) -> str:
    return (
        "You are a Chief Investment Officer. Analyze these 4 reports (Daily, Weekly, Monthly, Quarterly).\n"
        "1. Identify the ONE major conflict between the Short-Term (Daily) and Long-Term (Quarterly) view.\n"
        "2. Give me a single 'Executive Directive' for today based on this synthesis.\n"
        "3. Keep it under 200 words.\n\n"
        "[Daily]\n"
        f"{daily_text or ''}\n\n"
        "[Weekly]\n"
        f"{weekly_text or ''}\n\n"
        "[Monthly]\n"
        f"{monthly_text or ''}\n\n"
        "[Quarterly]\n"
        f"{quarterly_text or ''}\n"
    )


def _build_packet_prompt(intelligence_packet: str) -> str:
    return (
        "You are a report-event CIO synthesis engine. "
        "You receive a structured intelligence packet from Daily, Appendix, Weekly, Monthly, and Quarterly reports.\n\n"
        "Produce a concise executive synthesis (under 500 words) with these sections:\n"
        "- **MARKET RISK SUMMARY** (2-3 sentences referencing specific tickers and report evidence)\n"
        "- **TOP 3 ACTION ITEMS** (numbered, verb-first, each citing a ticker + specific data point)\n"
        "- **QUALITY WATCHLIST (TOP 3)** (durability, solvency, and capital-allocation lens)\n"
        "- **SIGNAL CONVERGENCE** (1 sentence connecting timeframes)\n\n"
        "Use ONLY the provided data. Never invent facts.\n\n"
        f"{intelligence_packet}"
    )


def _extract_text(resp: object) -> str:
    txt = (getattr(resp, "text", "") or "").strip()
    if txt:
        return txt
    cand = getattr(resp, "candidates", None) or []
    if cand and getattr(cand[0], "content", None):
        parts = getattr(cand[0].content, "parts", None) or []
        txt = " ".join((getattr(p, "text", "") or "").strip() for p in parts).strip()
    return txt


def _ollama_fallback(prompt: str) -> str:
    if ollama is None:
        return "Ollama unavailable: python 'ollama' package is not installed."
    model_candidates = ["gpt-oss:20b", "llama3:latest", "llama3", "llama3.1:latest", "llama3.1"]
    last_err: Exception | None = None
    for model in model_candidates:
        try:
            resp = ollama.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            txt = str(((resp or {}).get("message") or {}).get("content") or "").strip()
            if txt:
                return txt
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        return f"Ollama fallback failed: {str(last_err)[:220]}"
    return "Ollama fallback: empty response."


def _ollama_chat(model: str, prompt: str) -> str:
    if ollama is None:
        raise RuntimeError("python 'ollama' package is not installed")
    resp = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    txt = str(((resp or {}).get("message") or {}).get("content") or "").strip()
    if not txt:
        raise RuntimeError(f"empty response from model '{model}'")
    return txt


def _ollama_model_names() -> list[str]:
    if ollama is None:
        return []
    try:
        payload = ollama.list()
        models = (payload or {}).get("models") or []
        out: list[str] = []
        for m in models:
            name = str((m or {}).get("name") or "").strip()
            if name:
                out.append(name.lower())
        return out
    except Exception:
        return []


def _pick_ollama_model(candidates: list[str], fallback: str) -> str:
    names = _ollama_model_names()
    if not names:
        return fallback
    for c in candidates:
        if c.lower() in names:
            return c
    return fallback


def _local_consensus_fallback(
    daily_text: str,
    weekly_text: str,
    monthly_text: str,
    quarterly_text: str,
    intelligence_packet: str = "",
) -> str:
    if intelligence_packet.strip():
        shared = intelligence_packet
    else:
        shared = (
            "[Daily]\n"
            f"{daily_text or ''}\n\n"
            "[Weekly]\n"
            f"{weekly_text or ''}\n\n"
            "[Monthly]\n"
            f"{monthly_text or ''}\n\n"
            "[Quarterly]\n"
            f"{quarterly_text or ''}\n"
        )
    summary_prompt = (
        "You are a Chief Investment Officer. Synthesize this intelligence into a report-event executive brief. "
        "Reference specific tickers, dates, and concrete report evidence. "
        "Keep it concise, decision-focused, and under 400 words.\n\n"
        f"{shared}"
    )
    tactical_prompt = (
        "You are a tactical PM assistant. From this intelligence, produce today's 3 highest-priority tactical action items. "
        "Each must reference a specific ticker and data point. Use short bullets with verb-first phrasing.\n\n"
        f"{shared}"
    )

    summary_model = _pick_ollama_model(
        ["gpt-oss:20b", "llama3:latest", "llama3", "llama3.1:latest", "llama3.1"],
        fallback="llama3:latest",
    )
    risk_model = _pick_ollama_model(
        ["llama3:latest", "llama3", "llama3.1:latest", "llama3.1", "gpt-oss:20b"],
        fallback="gpt-oss:20b",
    )
    try:
        summary_txt = _ollama_chat(summary_model, summary_prompt)
    except Exception as e:
        summary_txt = f"Summary unavailable: {str(e)[:180]}"
    try:
        risk_txt = _ollama_chat(risk_model, tactical_prompt)
    except Exception as e:
        risk_txt = f"Tactical actions unavailable: {str(e)[:180]}"

    return (
        "Executive Synthesis (Local Consensus)\n\n"
        f"{summary_txt}\n\n"
        "Tactical Action Items\n"
        f"{risk_txt}"
    )


def synthesize_reports(
    daily_text: str,
    weekly_text: str,
    monthly_text: str,
    quarterly_text: str,
    intelligence_packet: str = "",
) -> str:
    # If an intelligence packet is provided, use it instead of raw report text
    if intelligence_packet.strip():
        prompt = _build_packet_prompt(intelligence_packet)
    else:
        prompt = _build_prompt(daily_text, weekly_text, monthly_text, quarterly_text)

    if genai is None:
        return _ollama_fallback(prompt)
    if genai_types is None:
        return _ollama_fallback(prompt)

    use_vertex = os.getenv("GEMINI_USE_VERTEX_AI", "0").strip().lower() in {"1", "true", "yes", "on"}
    if use_vertex:
        client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or os.getenv("GCP_PROJECT_ID", "").strip() or None,
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "europe-west1").strip(),
        )
    else:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            return _ollama_fallback(prompt)
        client = genai.Client(api_key=api_key)
    _model = os.getenv("GEMINI_FAST_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    last_err: Exception | None = None

    for attempt in range(1, 4):
        try:
            resp = client.models.generate_content(
                model=_model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=1200,
                ),
            )
            txt = _extract_text(resp)
            if txt:
                return txt
            last_err = RuntimeError("Gemini returned empty response.")
        except Exception as e:
            last_err = e
            msg = str(e)
            is_429 = ("429" in msg) or ("RESOURCE_EXHAUSTED" in msg.upper())
            if is_429 and attempt < 3:
                time.sleep(5 + random.uniform(0.0, 0.5))
                continue
            if not is_429:
                break

    if last_err is not None:
        msg = str(last_err)
        if ("429" in msg) or ("RESOURCE_EXHAUSTED" in msg.upper()):
            return _local_consensus_fallback(
                daily_text, weekly_text, monthly_text, quarterly_text,
                intelligence_packet=intelligence_packet,
            )

    return _ollama_fallback(prompt)
