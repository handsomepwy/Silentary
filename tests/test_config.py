"""Config loading: env > .env > config.json precedence."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_env_beats_dotenv_and_json(tmp_path, monkeypatch):
    """Precedence: real env var > .env file > JSON config file > default."""
    from app.config import load_settings

    monkeypatch.setenv("SILENTARY_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("SILENTARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SILENTARY_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SILENTARY_OWNER_TOKEN", "from-env-var")

    (tmp_path / "config.json").write_text(
        '{"owner_token": "from-json", "model": "openai/gpt-5"}', encoding="utf-8")

    st = load_settings()
    assert st.owner_token == "from-env-var"
    assert st.model == "openai/gpt-5"  # json supplies model (no .env in tmp_path)

    monkeypatch.setenv("SILENTARY_OWNER_TOKEN", "from-env-var-2")
    st = load_settings()
    assert st.owner_token == "from-env-var-2"


def test_dotenv_file_parsed(tmp_path, monkeypatch):
    """A root .env file is parsed with quote/export/comment handling."""
    import app.config as cfg

    dotenv = tmp_path / "root.env"
    dotenv.write_text(
        "# comment\n"
        "SILENTARY_OWNER_TOKEN='quoted-token'\n"
        'export SILENTARY_PROVIDER_API_KEY="sk-double"\n'
        "SILENTARY_MODEL=openai/gpt-5-mini\n",
        encoding="utf-8",
    )
    values = cfg._load_env_file(dotenv)
    assert values["SILENTARY_OWNER_TOKEN"] == "quoted-token"
    assert values["SILENTARY_PROVIDER_API_KEY"] == "sk-double"
    assert values["SILENTARY_MODEL"] == "openai/gpt-5-mini"

    # malformed line raises with a line number
    bad = tmp_path / "bad.env"
    bad.write_text("NOT VALID\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="line 1"):
        cfg._load_env_file(bad)


def test_dotenv_bad_line_raises(monkeypatch, tmp_path):
    """A malformed .env line fails loudly (config error, not silence)."""
    import app.config as cfg

    bad = tmp_path / ".env"
    bad.write_text("SILENTARY_OWNER_TOKEN=ok\nGARBAGE LINE\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="line 2"):
        cfg._load_env_file(bad)
