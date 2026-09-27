"""Project config, env handling, paths, PDK registry."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # loads .env if present, env vars take precedence

DEFAULT_MODEL = os.getenv(
    "OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"
)
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

PROJECT_FILE = "vlab_project.yaml"

# PDKs we bundle metadata for. AI auto-selects unless user overrides.
SUPPORTED_PDKS = ("sky130", "nangate45", "asap7")
DEFAULT_PDK_CHOICE = "auto"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pdks_registry_path() -> Path:
    return repo_root() / "pdks" / "registry.yaml"
