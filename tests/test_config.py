import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


@pytest.fixture
def dotenv_path(monkeypatch, tmp_path):
    monkeypatch.delenv("RAG_AI_ENV_FILE", raising=False)
    for key in (
        "POSTGRES_URL",
        "DOCS_DIR",
        "DOCS_IGNORE_PATTERNS",
        "RETRIEVAL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    path = tmp_path / "project.env"
    monkeypatch.setitem(Settings.model_config, "env_file", path)
    return path


def write_dotenv(path, *, timeout, docs_dir):
    path.write_text(
        f"POSTGRES_URL=postgresql:///unused\n"
        f"DOCS_DIR={docs_dir}\n"
        f"RETRIEVAL_TIMEOUT_SECONDS={timeout}\n"
        'DOCS_IGNORE_PATTERNS=\'["private notes", "*.tmp"]\'\n',
        encoding="utf-8",
    )


def test_default_dotenv_loads_shared_retrieval_and_ingestion_config(dotenv_path):
    write_dotenv(dotenv_path, timeout=2.5, docs_dir="/default")
    config = load_settings()
    assert config.retrieval_timeout_seconds == 2.5
    assert config.docs_dir == "/default"
    assert config.docs_ignore_patterns == ["private notes", "*.tmp"]


def test_custom_dotenv_replaces_default_and_environment_overrides(
    dotenv_path, monkeypatch
):
    write_dotenv(dotenv_path, timeout=2.5, docs_dir="/default")
    custom = dotenv_path.with_name("custom config.env")
    custom.write_text(
        "POSTGRES_URL=postgresql:///unused\n"
        "DOCS_DIR=/custom\n"
        "RETRIEVAL_TIMEOUT_SECONDS=4.5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_AI_ENV_FILE", str(custom))
    config = load_settings()
    assert config.retrieval_timeout_seconds == 4.5
    assert config.docs_dir == "/custom"
    assert (
        config.docs_ignore_patterns
        == Settings.model_fields["docs_ignore_patterns"].default
    )

    monkeypatch.setenv("RETRIEVAL_TIMEOUT_SECONDS", "0.75")
    monkeypatch.setenv("DOCS_IGNORE_PATTERNS", "[]")
    config = load_settings()
    assert config.retrieval_timeout_seconds == 0.75
    assert config.docs_ignore_patterns == []


def test_empty_selector_disables_dotenv(dotenv_path, monkeypatch):
    write_dotenv(dotenv_path, timeout="invalid", docs_dir="/default")
    monkeypatch.setenv("RAG_AI_ENV_FILE", "")
    monkeypatch.setenv("POSTGRES_URL", "postgresql:///unused")
    monkeypatch.setenv("DOCS_DIR", "/process")

    config = load_settings()
    assert config.retrieval_timeout_seconds == 10.0
    assert config.docs_dir == "/process"
    assert (
        config.docs_ignore_patterns
        == Settings.model_fields["docs_ignore_patterns"].default
    )


@pytest.mark.parametrize("value", ["0", "-1", "inf", "nan"])
def test_retrieval_timeout_must_be_positive_and_finite(dotenv_path, value):
    write_dotenv(dotenv_path, timeout=value, docs_dir="/default")
    with pytest.raises(ValidationError, match="retrieval_timeout_seconds"):
        load_settings()
