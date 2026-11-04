"""Settings for the help desk, read once from code/.env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

CODE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = CODE_DIR / "data"
PROMPTS_DIR = CODE_DIR / "prompts"

load_dotenv(CODE_DIR / ".env")


def _price(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    return float(value) if value else None


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    azure_endpoint: str
    azure_deployment: str
    azure_api_key: str
    reasoning_effort: str
    ollama_base_url: str
    ollama_chat_model: str
    ollama_reasoning_effort: str
    embedding_provider: str
    ollama_embedding_model: str
    azure_embedding_deployment: str
    feature_triage: bool
    feature_similar_tickets: bool
    feature_draft_reply: bool
    feature_assistant: bool
    db_path: Path
    llm_timeout_seconds: float
    llm_max_retries: int
    chat_price_input_per_mtok: float | None
    chat_price_output_per_mtok: float | None
    embedding_price_per_mtok: float | None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            llm_provider=env("LLM_PROVIDER", "azure").strip().lower(),
            azure_endpoint=env("AZURE_OPENAI_ENDPOINT", ""),
            azure_deployment=env("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini"),
            azure_api_key=env("AZURE_OPENAI_API_KEY", ""),
            reasoning_effort=env("REASONING_EFFORT", "").strip().lower(),
            ollama_base_url=env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            ollama_chat_model=env("OLLAMA_CHAT_MODEL", "qwen3:4b"),
            ollama_reasoning_effort=env("OLLAMA_REASONING_EFFORT", "none").strip().lower(),
            embedding_provider=env("EMBEDDING_PROVIDER", "ollama").strip().lower(),
            ollama_embedding_model=env("OLLAMA_EMBEDDING_MODEL", "embeddinggemma"),
            azure_embedding_deployment=env("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
            feature_triage=_flag("FEATURE_TRIAGE", default=True),
            feature_similar_tickets=_flag("FEATURE_SIMILAR_TICKETS", default=True),
            feature_draft_reply=_flag("FEATURE_DRAFT_REPLY", default=True),
            feature_assistant=_flag("FEATURE_ASSISTANT", default=True),
            db_path=CODE_DIR / env("HELPDESK_DB", "data/helpdesk.duckdb"),
            llm_timeout_seconds=float(env("LLM_TIMEOUT_SECONDS", "") or 60),
            llm_max_retries=int(env("LLM_MAX_RETRIES", "") or 3),
            chat_price_input_per_mtok=_price("CHAT_PRICE_INPUT_PER_MTOK"),
            chat_price_output_per_mtok=_price("CHAT_PRICE_OUTPUT_PER_MTOK"),
            embedding_price_per_mtok=_price("EMBEDDING_PRICE_PER_MTOK"),
        )


settings = Settings.from_env()
