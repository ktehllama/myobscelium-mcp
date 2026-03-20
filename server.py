import json
import os
import re
import yaml
from pathlib import Path
from datetime import date, datetime
from typing import Literal

try:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

from skills import register_skills

# --- Config ---
VAULT_PATH = Path(os.getenv("OBSIDIAN_VAULT_PATH", "~/Documents/Obsidian Vault")).resolve()
CHATS_FOLDER = os.getenv("OBSIDIAN_CHATS_FOLDER", "Claude/Chats")
DAILY_FOLDER = os.getenv("OBSIDIAN_DAILY_FOLDER", "Daily")

STOP_WORDS = {"the", "a", "an", "and", "or", "of", "in", "to"}
GENERIC_TAGS = {"chat", "claude", "conversation", "ai", "note"}
CONTENT_STOP = STOP_WORDS | {
    "about", "their", "there", "which", "would", "could", "should",
    "after", "before", "where", "what", "with", "from", "have",
    "that", "this", "some", "been", "were", "will", "into", "over",
    "more", "than", "them", "then", "they", "also", "each", "your",
    "using", "being", "when", "these", "those", "other", "between",
    "notes", "claude", "obsidian", "section", "content", "value",
}

mcp = FastMCP("obsidian")
register_skills(mcp)


# --- Path security ---
def vault_path(relative: str) -> Path:
    p = (VAULT_PATH / relative).resolve()
    if not p.is_relative_to(VAULT_PATH):
        raise ToolError(f"Path escapes vault: {relative}")
    return p


# --- Private helpers (shared by tools and obsidian_batch) ---

def _write_note(p: Path, content: str, overwrite: bool) -> bool:
    """Returns True if created new, False if overwritten."""
    existed = p.exists()
    if existed and not overwrite:
        raise ToolError(f"Note exists; set overwrite=true to replace: {p.relative_to(VAULT_PATH)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return not existed


def _move_note(src: Path, dst: Path) -> None:
    if not src.exists():
        raise ToolError(f"Source not found: {src.relative_to(VAULT_PATH)}")
    if dst.exists():
        raise ToolError(f"Destination exists: {dst.relative_to(VAULT_PATH)}. Delete it first.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def _delete_note(p: Path) -> None:
    if not p.exists():
        raise ToolError(f"Note not found: {p.relative_to(VAULT_PATH)}")
    p.unlink()


def _parse_frontmatter(p: Path) -> dict | None:
    """Read at most 16KB, extract and parse YAML frontmatter block."""
    with p.open(encoding="utf-8") as f:
        head = f.read(16384)
    if not head.startswith("---\n"):
        return None
    end = head.find("\n---", 4)
    if end == -1:
        return None
    block = head[4:end]
    try:
        return yaml.safe_load(block) or {}
    except yaml.YAMLError:
        return None


def _count_md(folder: Path) -> int:
    return sum(1 for f in folder.iterdir() if f.is_file() and f.suffix == ".md")


def _append_note(p: Path, content: str, add_separator: bool) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        existing = p.read_text(encoding="utf-8")
        if not existing.endswith("\n"):
            existing += "\n"
        sep = "\n---\n\n" if add_separator else "\n"
        p.write_text(existing + sep + content, encoding="utf-8")
    else:
        p.write_text(content, encoding="utf-8")


def _body_words(p: Path) -> set[str]:
    """Extract meaningful words from a note's body (first 1500 chars, 5+ chars, alpha only)."""
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        text = text[end + 4:] if end != -1 else text
    return {w for w in re.findall(r"[a-z]{5,}", text[:1500].lower()) if w not in CONTENT_STOP}


def _find_related_core(path_str: str, top_k: int = 10, folder: str = "", min_score: float = 0.05) -> dict:
    """Core similarity computation (IDF tags + title words + body content). Returns {"source": path_str, "related": [...]}."""
    target = vault_path(path_str)
    if not target.exists():
        raise ToolError(f"Note not found: {path_str}")
    root = vault_path(folder) if folder else VAULT_PATH
    if not root.is_dir():
        raise ToolError(f"Folder not found: {folder}")

    def _norm_tags(raw) -> set[str]:
        items = raw if isinstance(raw, list) else [raw]
        return {t for t in items if t and isinstance(t, str)}

    all_notes = []
    tag_freq: dict[str, int] = {}
    for md_file in root.rglob("*.md"):
        if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
            continue
        fm = _parse_frontmatter(md_file) or {}
        tags = _norm_tags(fm.get("tags", []))
        all_notes.append((md_file, tags))
        for t in tags:
            tag_freq[t] = tag_freq.get(t, 0) + 1

    target_fm = _parse_frontmatter(target) or {}
    target_tags = _norm_tags(target_fm.get("tags", []))
    target_words = set(re.findall(r"[a-z]+", target.stem.lower())) - STOP_WORDS
    target_body_words = _body_words(target)

    results = []
    for md_file, note_tags in all_notes:
        if md_file.resolve() == target.resolve():
            continue
        # Exclude generic tags from scoring entirely so they never contribute to relevance
        shared = (target_tags & note_tags) - GENERIC_TAGS
        tag_score = sum(1.0 / tag_freq[t] for t in shared)
        note_words = set(re.findall(r"[a-z]+", md_file.stem.lower())) - STOP_WORDS
        title_score = len(target_words & note_words) * 0.1
        content_score = len(target_body_words & _body_words(md_file)) * 0.03
        score = round(tag_score + title_score + content_score, 2)
        # Dampen same-folder matches — co-located notes need much stronger signal to link
        if md_file.parent == target.parent:
            score = round(score * 0.4, 2)
        if score > 0 and score >= min_score:
            rel = str(md_file.relative_to(VAULT_PATH))
            results.append({
                "p": rel,
                "title": md_file.stem,
                "wikilink": f"[[{md_file.stem}]]",
                "shared_tags": sorted(shared, key=lambda t: tag_freq[t]),
                "score": score,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"source": path_str, "related": results[:top_k]}


def _patch_section(p: Path, match: str, match_type: str, content: str,
                   heading_level: int | None = None, create_if_missing: bool = True) -> str:
    """Patch a section of a note. Returns 'ok' | 'created' | 'not_found'."""
    if not p.exists():
        raise ToolError(f"Note not found: {p.relative_to(VAULT_PATH)}")
    text = p.read_text(encoding="utf-8")

    if match_type == "text":
        if match not in text:
            return "not_found"
        p.write_text(text.replace(match, content, 1), encoding="utf-8")
        return "ok"

    if match_type == "section":
        # Remove the entire heading + body (content is ignored)
        removed = _remove_section(p, match, heading_level=heading_level)
        return "ok" if removed else "not_found"

    # match_type == "heading"
    heading_re = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
    lines = text.splitlines(keepends=True)
    found_idx = None
    found_level = None
    for i, line in enumerate(lines):
        m = heading_re.match(line.rstrip("\n").rstrip("\r"))
        if m:
            lvl = len(m.group(1))
            title = m.group(2)
            if title.lower() == match.lower():
                if heading_level is None or lvl == heading_level:
                    found_idx = i
                    found_level = lvl
                    break

    if found_idx is None:
        if not create_if_missing:
            return "not_found"
        lvl = heading_level or 2
        marker = "#" * lvl
        if text.endswith("\n\n"):
            sep = ""
        elif text.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        body = content if content.endswith("\n") else content + "\n"
        p.write_text(text + sep + f"{marker} {match}\n{body}", encoding="utf-8")
        return "created"

    # Find end of section: next heading of equal or higher level (fewer or equal #s)
    end_idx = len(lines)
    for i in range(found_idx + 1, len(lines)):
        m = heading_re.match(lines[i].rstrip("\n").rstrip("\r"))
        if m and len(m.group(1)) <= found_level:
            end_idx = i
            break

    heading_line = lines[found_idx]
    body = content if content.endswith("\n") else content + "\n"
    suffix_lines = lines[end_idx:]
    # Add blank line separator before the next heading if not already present
    if suffix_lines and not body.endswith("\n\n"):
        body = body + "\n"
    new_text = "".join(lines[:found_idx]) + heading_line + body + "".join(suffix_lines)
    p.write_text(new_text, encoding="utf-8")
    return "ok"


def _passes_tag_filter(shared_tags: list[str], target_stem: str, note_stem: str) -> bool:
    """Return True if the match is genuine — not just generic-tag overlap."""
    if any(t not in GENERIC_TAGS for t in shared_tags):
        return True
    # Fall back to title word overlap (alpha only — exclude date numbers like 2026, 03)
    t_words = set(re.findall(r"[a-z]+", target_stem.lower())) - STOP_WORDS
    n_words = set(re.findall(r"[a-z]+", note_stem.lower())) - STOP_WORDS
    return bool(t_words & n_words)


def _format_related_entry(title: str, shared_tags: list[str]) -> str:
    """Format a single ## Related bullet entry."""
    if shared_tags:
        non_generic = [t for t in shared_tags if t not in GENERIC_TAGS] or shared_tags[:2]
        tag_str = ", ".join(f"#{t}" for t in non_generic[:3])
        reason = f"shares {tag_str}"
    else:
        reason = "related by title"
    return f"* [[{title}]] — {reason}"


def _read_existing_related(text: str) -> tuple[list[str], set[str]]:
    """Find ## Related section and return (bullet_lines, set_of_wikilink_titles)."""
    lines = text.splitlines()
    related_start = None
    related_level = None
    for i, line in enumerate(lines):
        m = re.match(r'^(#{1,6})\s+(.*?)\s*$', line)
        if m and m.group(2).strip().lower() == "related":
            related_start = i
            related_level = len(m.group(1))
            break
    if related_start is None:
        return [], set()
    section_end = len(lines)
    for i in range(related_start + 1, len(lines)):
        m = re.match(r'^(#{1,6})\s+', lines[i])
        if m and len(m.group(1)) <= related_level:
            section_end = i
            break
    bullet_lines = []
    titles: set[str] = set()
    for line in lines[related_start + 1:section_end]:
        stripped = line.strip()
        if stripped.startswith("*"):
            m = re.search(r'\[\[([^\]]+)\]\]', stripped)
            if m:
                titles.add(m.group(1))
            bullet_lines.append(stripped)
    return bullet_lines, titles


def _apply_relink(note_path: Path, new_entries: list[str]) -> str:
    """Merge new_entries into the note's ## Related section. Returns 'updated' | 'no_change'."""
    text = note_path.read_text(encoding="utf-8")
    existing_lines, _ = _read_existing_related(text)

    # Dead link cleanup: keep only lines whose wikilink target exists in vault
    vault_stems = {
        f.stem for f in VAULT_PATH.rglob("*.md")
        if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
    }
    valid_existing = []
    for line in existing_lines:
        m = re.search(r'\[\[([^\]]+)\]\]', line)
        if m and m.group(1) in vault_stems:
            valid_existing.append(line)

    # Collect current titles from valid existing entries
    current_titles: set[str] = set()
    for line in valid_existing:
        m = re.search(r'\[\[([^\]]+)\]\]', line)
        if m:
            current_titles.add(m.group(1))

    # Dedup new entries against current
    to_add = []
    for entry in new_entries:
        m = re.search(r'\[\[([^\]]+)\]\]', entry)
        if m and m.group(1) not in current_titles:
            current_titles.add(m.group(1))
            to_add.append(entry)

    if not to_add and len(valid_existing) == len(existing_lines):
        return "no_change"

    merged = "\n".join(valid_existing + to_add)
    _patch_section(note_path, "Related", "heading", merged, heading_level=2, create_if_missing=True)
    return "updated"


def _remove_section(p: Path, match: str, heading_level: int | None = None) -> bool:
    """Remove an entire heading section (heading line + body). Returns True if found."""
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    heading_re = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
    lines = text.splitlines(keepends=True)
    found_idx = None
    found_level = None
    for i, line in enumerate(lines):
        m = heading_re.match(line.rstrip("\n").rstrip("\r"))
        if m:
            lvl = len(m.group(1))
            if m.group(2).lower() == match.lower():
                if heading_level is None or lvl == heading_level:
                    found_idx = i
                    found_level = lvl
                    break
    if found_idx is None:
        return False
    end_idx = len(lines)
    for i in range(found_idx + 1, len(lines)):
        m = heading_re.match(lines[i].rstrip("\n").rstrip("\r"))
        if m and len(m.group(1)) <= found_level:
            end_idx = i
            break
    prefix = "".join(lines[:found_idx]).rstrip("\n")
    suffix = "".join(lines[end_idx:])
    new_text = (prefix + "\n" if prefix else "") + suffix
    p.write_text(new_text, encoding="utf-8")
    return True


def _capture_related_state(note_path: Path) -> dict:
    """Snapshot the current ## Related section state of a note for undo."""
    text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    lines, _ = _read_existing_related(text)
    had = bool(re.search(r'^#{1,6}\s+Related\s*$', text, re.MULTILINE | re.IGNORECASE))
    return {
        "path": str(note_path.relative_to(VAULT_PATH)),
        "had_section": had,
        "section_content": "\n".join(lines) if had else None,
    }


# --- Tools ---

@mcp.tool()
def obsidian_vault_overview(mode: Literal["compact", "tree"] = "compact", max_depth: int = 3) -> dict:
    """Return vault folder structure."""
    if mode == "compact":
        folders = {}
        for item in sorted(VAULT_PATH.rglob("*")):
            if item.is_dir() and not any(part.startswith(".") for part in item.relative_to(VAULT_PATH).parts):
                rel = str(item.relative_to(VAULT_PATH))
                depth = rel.count(os.sep) + 1
                if depth <= max_depth:
                    folders[rel] = _count_md(item)
        return {"folders": folders}
    else:
        lines = []

        def walk(folder: Path, depth: int):
            if depth > max_depth:
                return
            for item in sorted(folder.iterdir()):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    indent = "  " * depth
                    lines.append(f"{indent}{item.name}/ ({_count_md(item)})")
                    walk(item, depth + 1)

        walk(VAULT_PATH, 0)
        return {"tree": "\n".join(lines)}


@mcp.tool()
def obsidian_read_note(path: str, line_start: int | None = None, line_end: int | None = None) -> dict:
    """Read a vault note, optionally by line range (1-indexed inclusive)."""
    p = vault_path(path)
    if not p.exists():
        raise ToolError(f"Note not found: {path}")
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    total = len(lines)
    if line_start is not None or line_end is not None:
        s = (line_start - 1) if line_start else 0
        e = line_end if line_end else total
        content = "".join(lines[s:e])
    else:
        content = "".join(lines)
    return {"p": path, "content": content, "total_lines": total}


@mcp.tool()
def obsidian_read_frontmatter(path: str) -> dict:
    """Read only the YAML frontmatter of a note."""
    p = vault_path(path)
    if not p.exists():
        raise ToolError(f"Note not found: {path}")
    fm = _parse_frontmatter(p)
    return {"p": path, "frontmatter": fm}


@mcp.tool()
def obsidian_write_note(path: str, content: str, overwrite: bool = False) -> dict:
    """Create or overwrite a vault note. Attempt directly — handle ToolError if it already exists rather than pre-checking."""
    p = vault_path(path)
    created = _write_note(p, content, overwrite)
    hint = None
    if path.endswith(".base"):
        hint = "Read skill://obsidian-bases before editing .base files"
    elif path.endswith(".canvas"):
        hint = "Read skill://json-canvas before editing .canvas files"
    result = {"p": path, "created": created}
    if hint:
        result["hint"] = hint
    return result


@mcp.tool()
def obsidian_append_to_note(path: str, content: str, add_separator: bool = False) -> dict:
    """Append content to a vault note, creating it if absent."""
    p = vault_path(path)
    _append_note(p, content, add_separator)
    return {"p": path, "appended_bytes": len(content.encode())}


@mcp.tool()
def obsidian_move_note(from_path: str, to_path: str) -> dict:
    """Move or rename a single note. Use obsidian_batch for multiple files, obsidian_move_folder for whole folders."""
    src = vault_path(from_path)
    dst = vault_path(to_path)
    _move_note(src, dst)
    return {"from": from_path, "to": to_path}


@mcp.tool()
def obsidian_delete_note(path: str, confirm: bool = False) -> dict:
    """Delete a vault note (confirm=true required)."""
    if not confirm:
        raise ToolError("Set confirm=true to delete a note.")
    p = vault_path(path)
    _delete_note(p)
    return {"p": path, "deleted": True}


@mcp.tool()
def obsidian_list_folder(folder: str = "", include_preview: bool = False, recursive: bool = False) -> dict:
    """List notes and subfolders. recursive=True returns all notes in the subtree as a flat list — use before obsidian_batch to get all paths at once."""
    p = vault_path(folder) if folder else VAULT_PATH
    if not p.is_dir():
        raise ToolError(f"Folder not found: {folder}")
    items = []
    if recursive:
        for md_file in sorted(p.rglob("*.md")):
            if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
                continue
            entry = {"p": str(md_file.relative_to(VAULT_PATH)), "m": int(md_file.stat().st_mtime * 1000)}
            if include_preview:
                try:
                    text = md_file.read_text(encoding="utf-8")
                    if text.startswith("---\n"):
                        end = text.find("\n---", 4)
                        text = text[end + 4:].lstrip() if end != -1 else text
                    entry["v"] = text[:120].replace("\n", " ")
                except (OSError, UnicodeDecodeError):
                    pass
            items.append(entry)
    else:
        for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                items.append({"name": item.name, "type": "folder", "notes": _count_md(item)})
            elif item.suffix == ".md":
                entry = {"name": item.name, "type": "note", "m": int(item.stat().st_mtime * 1000)}
                if include_preview:
                    try:
                        text = item.read_text(encoding="utf-8")
                        if text.startswith("---\n"):
                            end = text.find("\n---", 4)
                            text = text[end + 4:].lstrip() if end != -1 else text
                        entry["v"] = text[:120].replace("\n", " ")
                    except (OSError, UnicodeDecodeError):
                        pass
                items.append(entry)
    return {"folder": folder or "/", "items": items}


@mcp.tool()
def obsidian_save_chat(
    title: str,
    summary: str,
    content: str,
    tags: list[str] | None = None,
    project: str = "",
    folder: str = "",
) -> dict:
    """Save a Claude conversation for future Claude retrieval — not for human reading. content: ultra-dense structured notation. Use terse key:value pairs, short labels, symbols (→, ✓, ✗, !=). Include: decisions+rationale, non-obvious gotchas, final state of key values/configs, what failed and why. Omit: anything re-derivable from code, exploratory dead-ends, obvious steps. Format for fast machine parsing, not readability."""
    tags = tags or []
    target_folder = folder or CHATS_FOLDER
    today = date.today()
    filename = f"{today.strftime('%Y-%d-%m')} {title}.md"
    p = vault_path(f"{target_folder}/{filename}")
    p.parent.mkdir(parents=True, exist_ok=True)

    all_tags = ["claude", "chat"] + tags
    fm_data = {"date": today.strftime('%Y-%d-%m'), "tags": all_tags, "summary": summary, "source": "claude-desktop"}
    if project:
        fm_data["project"] = project
    frontmatter = "---\n" + yaml.dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip() + "\n---"

    if p.exists():
        now = datetime.now().strftime("%H:%M")
        section = f"\n\n## {now} — {title}\n\n{content}"
        existing = p.read_text(encoding="utf-8")
        p.write_text(existing + section, encoding="utf-8")
        return {"p": str(p.relative_to(VAULT_PATH)), "appended": True}
    else:
        full = f"{frontmatter}\n\n# {title}\n\n{content}"
        p.write_text(full, encoding="utf-8")
        return {"p": str(p.relative_to(VAULT_PATH)), "appended": False}


@mcp.tool()
def obsidian_search(
    query: str,
    folder: str = "",
    case_sensitive: bool = False,
    max_results: int = 20,
    max_scan: int = 500,
    literal: bool = True,
    group_by_file: bool = True,
    include_content: bool = False,
    content_max_chars: int = 2000,
    modified_after: str | None = None,
    modified_before: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Full-text search. group_by_file=True (default) returns one entry per file — avoids duplicate results. include_content=True adds full note text, eliminating N follow-up read_note calls. Set literal=False for regex. Filter by modified_after/before (YYYY-MM-DD) or tags."""
    root = vault_path(folder) if folder else VAULT_PATH
    flags = re.UNICODE if case_sensitive else re.IGNORECASE | re.UNICODE
    pattern = re.compile(re.escape(query) if literal else query, flags)

    after_ts = datetime.fromisoformat(modified_after).timestamp() if modified_after else None
    before_ts = datetime.fromisoformat(modified_before).timestamp() if modified_before else None

    results = []
    truncated = False
    scanned = 0

    for md_file in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
            continue
        if scanned >= max_scan or len(results) >= max_results:
            truncated = True
            break
        scanned += 1

        mtime = md_file.stat().st_mtime
        if after_ts and mtime < after_ts:
            continue
        if before_ts and mtime > before_ts:
            continue

        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if tags:
            fm = _parse_frontmatter(md_file)
            fm_tags = fm.get("tags", []) if fm else []
            if isinstance(fm_tags, str):
                fm_tags = [fm_tags]
            if not any(t in fm_tags for t in tags):
                continue

        rel = str(md_file.relative_to(VAULT_PATH))
        lines = text.splitlines()
        file_matches = [{"l": i, "v": line[:80]} for i, line in enumerate(lines, 1) if pattern.search(line)]

        if not file_matches:
            continue

        if group_by_file:
            entry = {"p": rel, "m": int(mtime * 1000), "matches": file_matches}
            if include_content:
                entry["content"] = text[:content_max_chars]
            results.append(entry)
        else:
            for m in file_matches:
                results.append({"p": rel, "l": m["l"], "v": m["v"]})

    return {"query": query, "files" if group_by_file else "matches": results, "truncated": truncated, "scanned": scanned}


@mcp.tool()
def obsidian_batch(operations: list[dict], confirm: bool = False) -> dict:
    """Preferred for 2+ write/move/delete/append ops — one tool call. confirm=True covers all deletes in the batch. No rollback on partial failure."""
    VALID_OPS = {"write", "move", "delete", "append", "find_related", "patch_section"}
    for i, op in enumerate(operations):
        op_type = op.get("op")
        if op_type not in VALID_OPS:
            raise ToolError(f"Operation {i}: invalid op '{op_type}'. Must be one of {VALID_OPS}")
        path = op.get("path")
        if not isinstance(path, str) or not path:
            raise ToolError(f"Operation {i}: 'path' must be a non-empty string")
        if op_type == "move":
            to = op.get("to")
            if not isinstance(to, str) or not to:
                raise ToolError(f"Operation {i}: 'to' must be a non-empty string for move")

    results = []
    for i, op in enumerate(operations):
        op_type = op["op"]
        try:
            if op_type == "write":
                p = vault_path(op["path"])
                created = _write_note(p, op.get("content") or "", op.get("overwrite", False))
                results.append({"index": i, "p": op["path"], "ok": True, "created": created})
            elif op_type == "append":
                p = vault_path(op["path"])
                _append_note(p, op.get("content") or "", op.get("add_separator", False))
                results.append({"index": i, "p": op["path"], "ok": True})
            elif op_type == "move":
                src = vault_path(op["path"])
                dst = vault_path(op["to"])
                _move_note(src, dst)
                results.append({"index": i, "from": op["path"], "to": op["to"], "ok": True})
            elif op_type == "delete":
                if not confirm and not op.get("confirm"):
                    raise ToolError("delete requires confirm=true on the batch or the individual op")
                p = vault_path(op["path"])
                _delete_note(p)
                results.append({"index": i, "p": op["path"], "ok": True})
            elif op_type == "patch_section":
                p = vault_path(op["path"])
                match_type = op.get("match_type", "heading")
                if match_type not in ("heading", "text", "section"):
                    raise ToolError(f"Operation {i}: match_type must be 'heading', 'text', or 'section'")
                status = _patch_section(
                    p,
                    op["match"],
                    match_type,
                    op.get("content", ""),
                    heading_level=op.get("heading_level"),
                    create_if_missing=op.get("create_if_missing", True),
                )
                results.append({"index": i, "p": op["path"], "ok": True, "status": status})
            elif op_type == "find_related":
                # find_related errors are per-op, never abort the batch
                try:
                    fr = _find_related_core(
                        op["path"],
                        top_k=op.get("max_results", 10),
                        folder=op.get("folder", ""),
                        min_score=op.get("min_score", 0.05),
                    )
                    results.append({"index": i, "p": op["path"], "ok": True, "related": fr["related"]})
                except Exception as e:
                    results.append({"index": i, "p": op["path"], "ok": False, "error": str(e)})
        except ToolError:
            raise
        except Exception as e:
            results.append({"index": i, "ok": False, "error": str(e)})

    success = sum(1 for r in results if r["ok"])
    return {"results": results, "success_count": success, "error_count": len(results) - success}


@mcp.tool()
def obsidian_patch_frontmatter(path: str, updates: dict, remove_keys: list[str] | None = None) -> dict:
    """Merge updates into a note's YAML frontmatter without touching the body. Use instead of read+write for tag/property changes."""
    p = vault_path(path)
    if not p.exists():
        raise ToolError(f"Note not found: {path}")
    text = p.read_text(encoding="utf-8")
    fm: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 4:]
    fm.update(updates)
    for key in (remove_keys or []):
        fm.pop(key, None)
    new_fm = "---\n" + yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip() + "\n---"
    p.write_text(new_fm + body, encoding="utf-8")
    return {"p": path, "frontmatter": fm}


@mcp.tool()
def obsidian_move_folder(from_folder: str, to_folder: str, overwrite: bool = False) -> dict:
    """Move an entire folder (all notes and subfolders) to a new location in one call."""
    src = vault_path(from_folder)
    dst = vault_path(to_folder)
    if not src.is_dir():
        raise ToolError(f"Source folder not found: {from_folder}")
    if dst.exists() and not overwrite:
        raise ToolError(f"Destination exists: {to_folder}. Set overwrite=true to merge into it.")
    dst.parent.mkdir(parents=True, exist_ok=True)

    moved = []
    skipped = []
    for src_file in sorted(src.rglob("*")):
        if any(part.startswith(".") for part in src_file.relative_to(VAULT_PATH).parts):
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        if src_file.is_dir():
            dst_file.mkdir(parents=True, exist_ok=True)
        else:
            if dst_file.exists() and not overwrite:
                skipped.append(str(rel))
                continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            src_file.rename(dst_file)
            moved.append(str(rel))

    # Remove now-empty source dirs (deepest first)
    for d in sorted(src.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        src.rmdir()
    except OSError:
        pass

    return {"from": from_folder, "to": to_folder, "moved": len(moved), "skipped": len(skipped)}


@mcp.tool()
def obsidian_find_related(
    path: str,
    top_k: int = 10,
    folder: str = "",
    min_score: float = 0.05,
) -> dict:
    """Discover notes topically related to a given note — use when you already have a note and want to find its neighbors, NOT when searching for a keyword (use obsidian_search for that). Scores by shared-tag rarity (IDF): ≥0.3 = strong overlap, 0.05–0.3 = moderate, <0.05 = weak noise. shared_tags lists the matching tags, rarest first. folder limits search to a vault subfolder. After calling: present wikilinks to the user, or read top hits with obsidian_read_note, or append a Related section with obsidian_append_to_note (add_separator=True). min_score default 0.05 filters weak matches; raise to 0.2–0.3 for only strong overlaps."""
    return _find_related_core(path, top_k=top_k, folder=folder, min_score=min_score)


@mcp.tool()
def obsidian_patch_section(
    path: str,
    match: str,
    match_type: Literal["heading", "text", "section"],
    content: str = "",
    heading_level: int | None = None,
    create_if_missing: bool = True,
) -> dict:
    """Surgical content editor. match_type='heading': replace a heading's body, preserving the heading line itself. match_type='section': delete the entire heading + body (content is ignored). match_type='text': raw find-and-replace for the first exact occurrence. heading_level (1–6) narrows heading matches to a specific level; None matches any level. create_if_missing=True (default, heading only) appends a new section if not found. Returns status: 'ok', 'created', or 'not_found'."""
    p = vault_path(path)
    status = _patch_section(p, match, match_type, content, heading_level=heading_level, create_if_missing=create_if_missing)
    return {"p": path, "status": status}


def _read_full_content(p: Path) -> str:
    """Read full note content, stripping frontmatter."""
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        text = text[end + 4:] if end != -1 else text
    return text.strip()


@mcp.tool()
def obsidian_relink(
    mode: Literal["normal", "full", "undo"] = "normal",
    min_score: float = 0.3,
    exclude_folders: list[str] | None = None,
    smart: bool = False,
) -> dict:
    """Automate building and maintaining ## Related sections across notes. mode='normal': updates only the most recently modified note in Claude/Chats/. mode='full': scans all notes in the vault (one-directional) and reports a summary. mode='undo': restores all notes to their state before the last run. exclude_folders: list of vault-relative folder paths to skip entirely (neither source nor target). smart=True (normal mode only): returns the target note and all heuristic candidates with their full content for the caller to evaluate — does NOT write anything; caller should review and use obsidian_patch_section to write the Related section. Same-folder notes are score-dampened (0.4x) to reduce project-folder noise. Scoring uses shared tags (IDF) + title word overlap + body content overlap — generic tags (chat, claude, ai, note, conversation) are excluded from scoring. min_score default 0.3."""
    undo_file = VAULT_PATH / ".relink-undo.json"
    excluded = [f.replace("\\", "/").rstrip("/") for f in (exclude_folders or [])]

    def _is_excluded(rel_path: str) -> bool:
        fwd = rel_path.replace("\\", "/")
        return any(fwd == ex or fwd.startswith(ex + "/") for ex in excluded)

    if mode == "undo":
        if not undo_file.exists():
            return {"mode": "undo", "status": "no_backup_found"}
        backup = json.loads(undo_file.read_text(encoding="utf-8"))
        restored = 0
        skipped = 0
        for entry in backup["entries"]:
            try:
                p = vault_path(entry["path"])
                if not p.exists():
                    skipped += 1
                    continue
                if entry["had_section"]:
                    _patch_section(p, "Related", "heading", entry["section_content"] or "",
                                   heading_level=2, create_if_missing=True)
                else:
                    _remove_section(p, "Related", heading_level=2)
                restored += 1
            except Exception:
                skipped += 1
        undo_file.unlink(missing_ok=True)
        return {"mode": "undo", "restored": restored, "skipped": skipped,
                "from_timestamp": backup.get("timestamp")}

    chats_path = vault_path(CHATS_FOLDER)

    if mode == "normal":
        if not chats_path.is_dir():
            raise ToolError(f"Chats folder not found: {CHATS_FOLDER}")
        notes = [
            f for f in chats_path.rglob("*.md")
            if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
        ]
        if not notes:
            return {"mode": "normal", "note": None, "status": "no_notes_found"}
        target = max(notes, key=lambda f: f.stat().st_mtime)
        target_str = str(target.relative_to(VAULT_PATH))
        heuristic_min = 0.05 if smart else min_score
        result = _find_related_core(target_str, top_k=20 if smart else 10, min_score=heuristic_min)
        related = [
            r for r in result["related"]
            if not _is_excluded(r["p"])
            and _passes_tag_filter(r["shared_tags"], target.stem, Path(r["p"]).stem)
        ]
        if smart:
            # Return full content for the caller (Claude) to evaluate — no writes
            return {
                "mode": "normal",
                "smart": True,
                "status": "review_pending",
                "target": target_str,
                "target_content": _read_full_content(target),
                "candidates": [
                    {**r, "content": _read_full_content(VAULT_PATH / r["p"])}
                    for r in related
                ],
                "instruction": (
                    "Review the target note and each candidate's full content. "
                    "Select only candidates that are GENUINELY related (specific topic/concept/workflow overlap, not just generic domain similarity). "
                    "Then call obsidian_patch_section with path=target, match='Related', match_type='heading', heading_level=2 "
                    "and content as a bullet list of [[wikilinks]] for the verified candidates."
                ),
            }
        entries = [_format_related_entry(r["title"], r["shared_tags"]) for r in related]
        if not entries:
            return {"mode": "normal", "note": target_str, "status": "no_matches"}
        # Save undo snapshot before patching
        backup = {"timestamp": datetime.now().isoformat(), "mode": "normal",
                  "entries": [_capture_related_state(target)]}
        undo_file.write_text(json.dumps(backup, indent=2), encoding="utf-8")
        status = _apply_relink(target, entries)
        return {"mode": "normal", "note": target_str, "status": status, "links_considered": len(entries)}

    # mode == "full"
    all_notes = [
        f for f in VAULT_PATH.rglob("*.md")
        if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
        and not _is_excluded(str(f.relative_to(VAULT_PATH)))
    ]

    # Collect forward links: note_path_str → [(title, shared_tags)]
    forward: dict[str, list[tuple[str, list[str]]]] = {}
    errors: list[dict] = []
    no_matches = 0

    if smart:
        raise ToolError("smart=True is only supported with mode='normal'")

    for note_file in all_notes:
        note_str = str(note_file.relative_to(VAULT_PATH))
        try:
            result = _find_related_core(note_str, top_k=10, min_score=min_score)
        except Exception as e:
            errors.append({"p": note_str, "error": str(e)})
            continue
        related = [
            r for r in result["related"]
            if not _is_excluded(r["p"])
            and _passes_tag_filter(r["shared_tags"], note_file.stem, Path(r["p"]).stem)
        ]
        filtered = [(r["title"], r["shared_tags"]) for r in related]
        if filtered:
            forward[note_str] = filtered
        else:
            no_matches += 1

    # Save undo snapshot before patching
    backup_entries = [_capture_related_state(vault_path(ns)) for ns in forward]
    undo_file.write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "mode": "full", "entries": backup_entries}, indent=2),
        encoding="utf-8",
    )

    updated = 0
    no_change = 0
    for note_str, entries in forward.items():
        try:
            note_file = vault_path(note_str)
            formatted = [_format_related_entry(title, tags) for title, tags in entries]
            status = _apply_relink(note_file, formatted)
            if status == "updated":
                updated += 1
            else:
                no_change += 1
        except Exception as e:
            errors.append({"p": note_str, "error": str(e)})

    return {
        "mode": "full",
        "updated": updated,
        "no_change": no_change,
        "no_matches": no_matches,
        "errored": len(errors),
        "errors": errors,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
