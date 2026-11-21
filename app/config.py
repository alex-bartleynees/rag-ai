import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Postgres
    postgres_url: str

    # Ollama
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "llama3.2"
    llm_api_key: str = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768

    dependency_timeout_seconds: float = Field(default=2, gt=0)
    embedding_probe_timeout_seconds: float = Field(default=60, gt=0)
    startup_retry_seconds: float = Field(default=1, gt=0)
    startup_retry_max_seconds: float = Field(default=30, gt=0)
    retrieval_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_retry_delays(self):
        if self.startup_retry_max_seconds < self.startup_retry_seconds:
            raise ValueError("Startup retry maximum must be at least the initial delay")
        return self

    # Ingestion
    docs_dir: str
    docs_ignore_patterns: list[str] = [
        ".obsidian",
        ".git",
        ".trash",
        ".tmp-*",
        "*.tmp",
        "~*",
    ]
    plain_text_formats: list[str] = ["txt", "md"]
    markup_formats: list[str] = ["html"]
    supported_file_types: list[str] = plain_text_formats + markup_formats
    chunk_size: int = 500
    chunk_overlap: int = 50

    # Semantic cache
    cache_similarity_threshold: float = 0.15
    cache_ttl_seconds: int = 86400

    # Sessions
    session_ttl_seconds: int = 86400

    # Logging
    log_level: str = "INFO"
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def load_settings() -> Settings:
    env_file = os.environ.get("RAG_AI_ENV_FILE")
    if env_file is None:
        return Settings()
    return Settings(_env_file=env_file or None)


settings = load_settings()
