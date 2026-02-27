#!/usr/bin/env python3
"""Runtime AI configuration for terminal intelligence modules."""

from __future__ import annotations

import os


# Provider routing:
# - "openai": cloud default for highest reasoning quality / reliability
# - "anthropic": optional cloud alternative
# - "ollama": local model on http://127.0.0.1:11434
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").strip().lower()

# Fallback provider used automatically on primary failure.
AI_FALLBACK_PROVIDER = os.getenv("AI_FALLBACK_PROVIDER", "ollama").strip().lower()

# Cloud model defaults (cost-aware, accuracy-first baseline).
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_FALLBACK_MODELS = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.0-flash,gemini-1.5-flash").strip()

# Local model defaults.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3").strip()
OLLAMA_SMART_MODEL = os.getenv("OLLAMA_SMART_MODEL", "gemma2:27b").strip()
OLLAMA_FAST_MODEL = os.getenv("OLLAMA_FAST_MODEL", "gemma2:9b").strip()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate").strip()

# Shared request limits.
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "90").strip())
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "3000").strip())
