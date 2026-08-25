# obsidian_read_note Anchor-Range Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add anchor-based range reading to `obsidian_read_note` — callers specify start/end text anchors (with optional line offsets) instead of line numbers.

**Architecture:** Add a private `_resolve_anchor(lines, anchor_text, offset)` helper that locates the first line starting with `anchor_text` and applies the integer offset. Add four new optional params to `obsidian_read_note`; when `anchor_start` is set the function bypasses the summary tier and slices using the resolved anchor positions.

**Tech Stack:** Python 3.11+, pytest (new dep), existing `ToolError` from fastmcp.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `server.py` | Modify | Add `_resolve_anchor` helper (~line 618, before `obsidian_read_note`). Add 4 params + anchor-mode branch to `obsidian_read_note` (~line 620). Update docstring. |
| `tests/test_read_note_anchor.py` | Create | All tests for the new feature |
| `requirements.txt` | Modify | Add `pytest` |

---

## Task 1: Install pytest and create test file scaffold

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/test_read_note_anchor.py`

- [ ] **Step 1: Install pytest**

```bash
venv/Scripts/pip install pytest
```

Expected output: `Successfully installed pytest-...`

- [ ] **Step 2: Add pytest to requirements.txt**

Append to `requirements.txt`:
```
pytest
```

- [ ] **Step 3: Create `tests/__init__.py`** (empty file)

- [ ] **Step 4: Create `tests/test_read_note_anchor.py` scaffold**

```python
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
    p.write_text(content, encoding="utf-8")
    return name
```

- [ ] **Step 5: Commit scaffold**

```bash
git add requirements.txt tests/__init__.py tests/test_read_note_anchor.py
git commit -m "test: add pytest and test scaffold for anchor-range feature"
```

---

## Task 2: Write failing tests

**Files:**
- Modify: `tests/test_read_note_anchor.py`

All tests go in the same file. Add each test function below the scaffold from Task 1.

- [ ] **Step 1: Write tests**

```python
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
    content = "## A\n## B\n"
    path = make_note(vault, "note.md", content)
    # anchor_start at index 0, offset +5 → effective start = 5
    # anchor_end at index 1, offset 0  → effective end = 1
    # start > end → error
    with pytest.raises(Exception, match="empty"):
        server.obsidian_read_note(
            path, anchor_start="## A", anchor_start_offset=5, anchor_end="## B"
        )
```

- [ ] **Step 2: Run tests — verify they all FAIL**

```bash
venv/Scripts/python -m pytest tests/test_read_note_anchor.py -v 2>&1 | head -60
```

Expected: multiple failures like `AttributeError: obsidian_read_note() got an unexpected keyword argument 'anchor_start'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_read_note_anchor.py
git commit -m "test: add failing tests for anchor-range read feature"
```

---

## Task 3: Add `_resolve_anchor` helper to server.py

**Files:**
- Modify: `server.py` — insert before `def obsidian_read_note` (~line 620)

- [ ] **Step 1: Insert `_resolve_anchor` above `obsidian_read_note`**

Find the line `def obsidian_read_note(` (around line 620) and insert this block immediately before it:

```python
def _resolve_anchor(lines: list[str], anchor_text: str, offset: int) -> int:
    """Return the effective line index for an anchor.

    Scans lines for the first line that starts with anchor_text (case-sensitive).
    Applies offset (positive = down, negative = up). Clamps to [0, len(lines)-1].
    Raises ToolError if anchor_text is not found.
    """
    for i, line in enumerate(lines):
        if line.startswith(anchor_text):
            return max(0, min(len(lines) - 1, i + offset))
    raise ToolError(f"Anchor not found: {anchor_text!r}")


```

- [ ] **Step 2: Run tests — some should now pass, error tests should pass**

```bash
venv/Scripts/python -m pytest tests/test_read_note_anchor.py -v 2>&1 | head -60
```

Expected: error tests still fail (anchor params not wired yet), but `_resolve_anchor` is importable without crash.

---

## Task 4: Wire anchor params into `obsidian_read_note`

**Files:**
- Modify: `server.py` — `obsidian_read_note` function signature + body

- [ ] **Step 1: Update the function signature**

Replace:
```python
def obsidian_read_note(
    path: str,
    full: bool = False,
    line_start: int | None = None,
    line_end: int | None = None,
) -> dict:
```

With:
```python
def obsidian_read_note(
    path: str,
    full: bool = False,
    line_start: int | None = None,
    line_end: int | None = None,
    anchor_start: str | None = None,
    anchor_start_offset: int = 0,
    anchor_end: str | None = None,
    anchor_end_offset: int = 0,
) -> dict:
```

- [ ] **Step 2: Update the docstring**

Replace the existing docstring body with:

```python
    """Read a vault note.

    Default (full=False): returns the note's l0 and l1 summary fields — a cheap orientation
    layer, NOT the full file. l0 is a single sentence; l1 is 2-3 sentences. Use this first.
    If the note has no summary fields, automatically returns the full content instead.

    If you need more context after reading the summary, call again with full=True.
    NEVER stay with doubts — even the tiniest uncertainty about the note's content means
    you should call full=True immediately. Correctness always supersedes token efficiency.

    full=True supports line_start/line_end to read a specific line range.

    Anchor mode: set anchor_start to a string that the target line must start with
    (case-sensitive, first match). The slice runs from that line to anchor_end (or EOF).
    Both anchors support an integer offset to shift the effective boundary up (negative)
    or down (positive). Both effective boundaries are inclusive. Raises ToolError if an
    anchor is not found or if the effective range is empty after applying offsets.
    """
```

- [ ] **Step 3: Add anchor-mode branch in the function body**

After `text = p.read_text(encoding="utf-8")` and `lines = text.splitlines(keepends=True)`, and **before** the `if not full:` summary block, insert:

```python
    if anchor_start is not None:
        start_idx = _resolve_anchor(lines, anchor_start, anchor_start_offset)
        if anchor_end is not None:
            end_idx = _resolve_anchor(lines, anchor_end, anchor_end_offset)
        else:
            end_idx = total - 1
        if start_idx > end_idx:
            raise ToolError(
                f"Anchor range is empty after applying offsets "
                f"(effective start={start_idx + 1}, end={end_idx + 1})"
            )
        content = "".join(lines[start_idx : end_idx + 1])
        return {"p": path, "content": content, "total_lines": total}
```

The function body after changes should read (showing the relevant section):

```python
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    total = len(lines)

    if anchor_start is not None:
        start_idx = _resolve_anchor(lines, anchor_start, anchor_start_offset)
        if anchor_end is not None:
            end_idx = _resolve_anchor(lines, anchor_end, anchor_end_offset)
        else:
            end_idx = total - 1
        if start_idx > end_idx:
            raise ToolError(
                f"Anchor range is empty after applying offsets "
                f"(effective start={start_idx + 1}, end={end_idx + 1})"
            )
        content = "".join(lines[start_idx : end_idx + 1])
        return {"p": path, "content": content, "total_lines": total}

    if not full:
        # ... existing summary-tier code ...
```

- [ ] **Step 4: Run all tests — all should pass**

```bash
venv/Scripts/python -m pytest tests/test_read_note_anchor.py -v
```

Expected output:
```
PASSED tests/test_read_note_anchor.py::test_anchor_start_only_reads_to_eof
PASSED tests/test_read_note_anchor.py::test_anchor_start_and_end_inclusive
PASSED tests/test_read_note_anchor.py::test_anchor_end_offset_negative
PASSED tests/test_read_note_anchor.py::test_anchor_start_offset_positive
PASSED tests/test_read_note_anchor.py::test_anchor_start_offset_negative
PASSED tests/test_read_note_anchor.py::test_anchor_bypasses_summary_tier
PASSED tests/test_read_note_anchor.py::test_anchor_match_is_startswith_not_contains
PASSED tests/test_read_note_anchor.py::test_anchor_match_is_case_sensitive
PASSED tests/test_read_note_anchor.py::test_offset_clamps_to_file_bounds
PASSED tests/test_read_note_anchor.py::test_anchor_start_not_found_raises
PASSED tests/test_read_note_anchor.py::test_anchor_end_not_found_raises
PASSED tests/test_read_note_anchor.py::test_empty_range_after_offsets_raises
12 passed in ...
```

- [ ] **Step 5: Commit implementation**

```bash
git add server.py
git commit -m "feat: add anchor-based range reading to obsidian_read_note"
```

---

## Task 5: Update sanitized copy and finalize

**Files:**
- Modify: `sanitized/server.py` — sync the same changes

The project maintains a `sanitized/server.py` with `VAULT_PATH` restored to a placeholder. Apply the same `_resolve_anchor` helper and param changes there.

- [ ] **Step 1: Verify sanitized/server.py has the same read_note signature**

```bash
grep -n "def obsidian_read_note" sanitized/server.py
```

- [ ] **Step 2: Apply the same changes to sanitized/server.py**

Repeat Task 3 Step 1 and Task 4 Steps 1–3 in `sanitized/server.py`.

- [ ] **Step 3: Commit sanitized copy**

```bash
git add sanitized/server.py
git commit -m "chore: sync anchor-range changes to sanitized/server.py"
```

---

## Self-Review

**Spec coverage:**
- [x] 4 new params: `anchor_start`, `anchor_start_offset`, `anchor_end`, `anchor_end_offset` — Task 4
- [x] startswith matching, case-sensitive — `_resolve_anchor` in Task 3
- [x] First match wins — `_resolve_anchor` returns on first hit
- [x] Offsets shift effective boundary — `i + offset` in `_resolve_anchor`
- [x] Both boundaries inclusive — `lines[start_idx : end_idx + 1]`
- [x] `anchor_end=None` reads to EOF — `end_idx = total - 1`
- [x] Raises on anchor not found — `ToolError` in `_resolve_anchor`
- [x] Raises on empty range — `start_idx > end_idx` check
- [x] Clamps on out-of-bounds offset — `max(0, min(len(lines)-1, ...))` in `_resolve_anchor`
- [x] Bypasses summary tier — anchor branch runs before `if not full:` block
- [x] Existing behavior unchanged — no modification to summary or line-range paths
- [x] sanitized/server.py synced — Task 5

**Placeholder scan:** None found.

**Type consistency:** `_resolve_anchor` returns `int`; used as `start_idx`/`end_idx` in slice — consistent throughout.
