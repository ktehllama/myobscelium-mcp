"""Tests for obsidian_read_note anchor-based range reading."""
import pytest
from pathlib import Path
import server


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Point VAULT_PATH at a temp directory and return it."""
    monkeypatch.setattr(server, "VAULT_PATH", tmp_path)
    return tmp_path


def make_note(vault: Path, name: str, content: str) -> str:
    """Write content to vault/name and return the relative path string."""
    p = vault / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return name
