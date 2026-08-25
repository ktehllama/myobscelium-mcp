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


# --- Happy-path tests ---

def test_anchor_start_only_reads_to_eof(vault):
    """anchor_start with no anchor_end returns from anchor to end of file."""
    content = "line 1\n## START\nline 3\nline 4\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(path, anchor_start="## START")
    assert result["content"] == "## START\nline 3\nline 4\n"
    assert result["total_lines"] == 4


def test_anchor_start_and_end_inclusive(vault):
    """Both anchors are included in the returned slice."""
    content = "before\n## A\nmiddle\n## B\nafter\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(path, anchor_start="## A", anchor_end="## B")
    assert result["content"] == "## A\nmiddle\n## B\n"


def test_anchor_end_offset_negative(vault):
    """anchor_end_offset=-2 ends 2 lines before the end anchor."""
    content = "## A\nrow1\nrow2\n---\n\n## B\n"
    path = make_note(vault, "note.md", content)
    # ## B is line index 5 (0-based); -2 → index 3 → "---\n"
    result = server.obsidian_read_note(
        path, anchor_start="## A", anchor_end="## B", anchor_end_offset=-2
    )
    assert result["content"] == "## A\nrow1\nrow2\n---\n"


def test_anchor_start_offset_positive(vault):
    """anchor_start_offset=1 skips the anchor line itself."""
    content = "## A\nfirst\nsecond\n## B\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(
        path, anchor_start="## A", anchor_start_offset=1, anchor_end="## B"
    )
    assert result["content"] == "first\nsecond\n## B\n"


def test_anchor_start_offset_negative(vault):
    """anchor_start_offset=-1 includes the line before the start anchor."""
    content = "preamble\n## A\ncontent\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(
        path, anchor_start="## A", anchor_start_offset=-1
    )
    assert result["content"] == "preamble\n## A\ncontent\n"


def test_anchor_bypasses_summary_tier(vault):
    """Providing anchor_start returns content even when full=False (summary bypass)."""
    content = "---\nl0: short summary\nl1: longer summary\n---\n## A\nbody\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(path, anchor_start="## A")
    assert result["content"] == "## A\nbody\n"


def test_anchor_match_is_startswith_not_contains(vault):
    """A line containing the anchor text mid-line does NOT match."""
    content = "no match here ## A\n## A\nreal match\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(path, anchor_start="## A")
    # First real startswith match is line index 1
    assert result["content"] == "## A\nreal match\n"


def test_anchor_match_is_case_sensitive(vault):
    """Match is case-sensitive: '## a' does not match '## A'."""
    content = "## a\n## A\ncontent\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(path, anchor_start="## A")
    assert result["content"] == "## A\ncontent\n"


def test_offset_clamps_to_file_bounds(vault):
    """A large negative start offset clamps to line 0 without error."""
    content = "## A\ncontent\n"
    path = make_note(vault, "note.md", content)
    result = server.obsidian_read_note(path, anchor_start="## A", anchor_start_offset=-99)
    assert result["content"] == "## A\ncontent\n"


# --- Error tests ---

def test_anchor_start_not_found_raises(vault):
    content = "line1\nline2\n"
    path = make_note(vault, "note.md", content)
    with pytest.raises(Exception, match="Anchor not found"):
        server.obsidian_read_note(path, anchor_start="## MISSING")


def test_anchor_end_not_found_raises(vault):
    content = "## A\ncontent\n"
    path = make_note(vault, "note.md", content)
    with pytest.raises(Exception, match="Anchor not found"):
        server.obsidian_read_note(path, anchor_start="## A", anchor_end="## MISSING")


def test_empty_range_after_offsets_raises(vault):
    """Effective start > effective end raises ToolError."""
    content = "line0\n## A\nline2\n## B\n"
    path = make_note(vault, "note.md", content)
    # ## A at index 1, offset +3 → clamped to index 3 (effective start = 3)
    # ## B at index 3, offset -2 → index 1 (effective end = 1)
    # start (3) > end (1) → error
    with pytest.raises(Exception, match="empty"):
        server.obsidian_read_note(
            path, anchor_start="## A", anchor_start_offset=3,
            anchor_end="## B", anchor_end_offset=-2,
        )
