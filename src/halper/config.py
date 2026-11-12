"""Settings read from environment variables. This is the only place model IDs live."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Model ID from the Anthropic models overview (checked 2026-11-12).
DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    max_turns: int = 10
    max_tokens: int = 4096
    max_retries: int = 5
    notes_dir: str = "sample_notes"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from `env` (defaults to os.environ). Unset variables keep defaults."""
    e = os.environ if env is None else env
    d = Settings()
    return Settings(
        model=e.get("AGENT_MODEL") or d.model,
        max_turns=int(e.get("AGENT_MAX_TURNS") or d.max_turns),
        max_tokens=int(e.get("AGENT_MAX_TOKENS") or d.max_tokens),
        max_retries=int(e.get("AGENT_MAX_RETRIES") or d.max_retries),
        notes_dir=e.get("AGENT_NOTES_DIR") or d.notes_dir,
    )
