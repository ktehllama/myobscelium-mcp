import asyncio
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

# --- Named constants ---
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SAME_FOLDER_DAMPENING = 0.4
SAME_FOLDER_MAX_LINKS = 1  # max links to notes in the same folder (prevents session blobs)
CROSS_PROJECT_DAMPENING = 0.3  # score multiplier when both notes have different non-empty project fields
RELINK_MIN_SCORE = 0.3
FIND_RELATED_MIN_SCORE = 0.05
TITLE_WORD_WEIGHT = 0.1
BODY_WORD_WEIGHT = 0.03
BODY_WORDS_MAX_CHARS = 1500
FRONTMATTER_MAX_BYTES = 16384
SEARCH_PREVIEW_LEN = 120

# --- Abbreviated response key constants ---
K_PATH, K_CONTENT, K_MODIFIED, K_LINES, K_SCORE, K_TAGS, K_L0 = "p", "c", "m", "lc", "s", "st", "l0"

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
    """Read at most FRONTMATTER_MAX_BYTES, extract and parse YAML frontmatter block."""
    with p.open(encoding="utf-8") as f:
        head = f.read(FRONTMATTER_MAX_BYTES)
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
    """Extract meaningful words from a note's body (first BODY_WORDS_MAX_CHARS chars, 5+ chars, alpha only)."""
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        text = text[end + 4:] if end != -1 else text
    return {w for w in re.findall(r"[a-z]{5,}", text[:BODY_WORDS_MAX_CHARS].lower()) if w not in CONTENT_STOP}


def _infer_project(note_path: Path, fm: dict) -> str:
    """Return the project context for a note — frontmatter first, then Projects/<Name>/ folder inference."""
    p = str(fm.get("project", "") or "").strip().lower()
    if p:
        return p
    parts = note_path.relative_to(VAULT_PATH).parts
    for i, part in enumerate(parts):
        if part.lower() == "projects" and i + 1 < len(parts) - 1:
            return parts[i + 1].lower()
    return ""


def _find_related_core(path_str: str, top_k: int = 10, folder: str = "", min_score: float = FIND_RELATED_MIN_SCORE) -> dict:
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
    title_word_freq: dict[str, int] = {}
    for md_file in root.rglob("*.md"):
        if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
            continue
        fm = _parse_frontmatter(md_file) or {}
        tags = _norm_tags(fm.get("tags", []))
        l0 = str(fm.get("l0", "") or "")
        project = _infer_project(md_file, fm)
        all_notes.append((md_file, tags, l0, project))
        for t in tags:
            tag_freq[t] = tag_freq.get(t, 0) + 1
        for w in set(re.findall(r"[a-z]{3,}", md_file.stem.lower())) - CONTENT_STOP:
            title_word_freq[w] = title_word_freq.get(w, 0) + 1

    target_fm = _parse_frontmatter(target) or {}
    target_tags = _norm_tags(target_fm.get("tags", []))
    target_words = set(re.findall(r"[a-z]{3,}", target.stem.lower())) - CONTENT_STOP
    target_body_words = _body_words(target)
    target_project = _infer_project(target, target_fm)

    results = []
    for md_file, note_tags, note_l0, note_project in all_notes:
        if md_file.resolve() == target.resolve():
            continue
        shared = (target_tags & note_tags) - GENERIC_TAGS
        tag_score = sum(1.0 / tag_freq[t] for t in shared)
        note_words = set(re.findall(r"[a-z]{3,}", md_file.stem.lower())) - CONTENT_STOP
        shared_title = target_words & note_words
        title_score = sum(1.0 / title_word_freq.get(w, 1) for w in shared_title) * TITLE_WORD_WEIGHT
        content_score = len(target_body_words & _body_words(md_file)) * BODY_WORD_WEIGHT
        score = round(tag_score + title_score + content_score, 2)
        if md_file.parent == target.parent:
            score = round(score * SAME_FOLDER_DAMPENING, 2)
        if target_project and note_project and target_project != note_project:
            score = round(score * CROSS_PROJECT_DAMPENING, 2)
        if score > 0 and score >= min_score:
            rel = str(md_file.relative_to(VAULT_PATH))
            results.append({
                K_PATH: rel,
                "title": md_file.stem,
                "wikilink": f"[[{md_file.stem}]]",
                "shared_tags": sorted(shared, key=lambda t: tag_freq[t]),
                "score": score,
                K_L0: note_l0,
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
        removed = _remove_section(p, match, heading_level=heading_level)
        return "ok" if removed else "not_found"

    # match_type == "heading"
    # Strip any leading # markers the caller may have included (e.g. "## Related" → "Related")
    clean_match = re.sub(r'^#+\s*', '', match).strip()
    heading_re = re.compile(r'^(#{1,6})\s+(.*?)\s*$')
    lines = text.splitlines(keepends=True)
    found_idx = None
    found_level = None
    for i, line in enumerate(lines):
        m = heading_re.match(line.rstrip("\n").rstrip("\r"))
        if m:
            lvl = len(m.group(1))
            title = m.group(2)
            if title.lower() == clean_match.lower():
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
        p.write_text(text + sep + f"{marker} {clean_match}\n{body}", encoding="utf-8")
        return "created"

    end_idx = len(lines)
    for i in range(found_idx + 1, len(lines)):
        m = heading_re.match(lines[i].rstrip("\n").rstrip("\r"))
        if m and len(m.group(1)) <= found_level:
            end_idx = i
            break

    heading_line = lines[found_idx]
    body = content if content.endswith("\n") else content + "\n"
    suffix_lines = lines[end_idx:]
    if suffix_lines and not body.endswith("\n\n"):
        body = body + "\n"
    new_text = "".join(lines[:found_idx]) + heading_line + body + "".join(suffix_lines)
    p.write_text(new_text, encoding="utf-8")
    return "ok"


def _cap_same_folder_links(related: list, target_parent: Path, max_links: int = SAME_FOLDER_MAX_LINKS) -> list:
    """Keep only max_links results from the same folder as target. Cross-folder links unrestricted.
    Input must be pre-sorted by score descending (as returned by _find_related_core)."""
    same_count = 0
    out = []
    for r in related:
        if (VAULT_PATH / r[K_PATH]).parent == target_parent:
            if same_count >= max_links:
                continue
            same_count += 1
        out.append(r)
    return out


def _passes_tag_filter(shared_tags: list[str], target_stem: str, note_stem: str) -> bool:
    """Return True if the match is genuine — not just generic-tag overlap."""
    if any(t not in GENERIC_TAGS for t in shared_tags):
        return True
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


def _apply_relink(note_path: Path, new_entries: list[str], vault_stems: set[str] | None = None) -> str:
    """Merge new_entries into the note's ## Related section. Returns 'updated' | 'no_change'."""
    text = note_path.read_text(encoding="utf-8")

    # Collapse any duplicate Related sections — keep only the first occurrence's content,
    # remove all subsequent ones before patching to prevent accumulation.
    related_heading_re = re.compile(r'^(#{1,6})\s+Related\s*$', re.IGNORECASE)
    any_heading_re = re.compile(r'^#{1,6}\s+')
    raw_lines = text.splitlines(keepends=True)
    related_indices = [i for i, l in enumerate(raw_lines) if related_heading_re.match(l.rstrip())]
    if len(related_indices) > 1:
        # Remove all but the first: walk backwards so indices stay valid
        for start in reversed(related_indices[1:]):
            lvl = len(raw_lines[start]) - len(raw_lines[start].lstrip('#'))
            end = len(raw_lines)
            for j in range(start + 1, len(raw_lines)):
                m = any_heading_re.match(raw_lines[j])
                if m and (len(raw_lines[j]) - len(raw_lines[j].lstrip('#'))) <= lvl:
                    end = j
                    break
            del raw_lines[start:end]
        text = "".join(raw_lines)
        note_path.write_text(text, encoding="utf-8")

    existing_lines, _ = _read_existing_related(text)

    if vault_stems is None:
        vault_stems = {
            f.stem for f in VAULT_PATH.rglob("*.md")
            if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
        }

    valid_existing = []
    for line in existing_lines:
        m = re.search(r'\[\[([^\]]+)\]\]', line)
        if m and m.group(1) in vault_stems:
            valid_existing.append(line)

    current_titles: set[str] = set()
    for line in valid_existing:
        m = re.search(r'\[\[([^\]]+)\]\]', line)
        if m:
            current_titles.add(m.group(1))

    to_add = []
    for entry in new_entries:
        m = re.search(r'\[\[([^\]]+)\]\]', entry)
        if m and m.group(1) not in current_titles:
            current_titles.add(m.group(1))
            to_add.append(entry)

    if not to_add and len(valid_existing) == len(existing_lines):
        return "no_change"

    merged = "\n".join(valid_existing + to_add)
    _patch_section(note_path, "Related", "heading", merged, heading_level=None, create_if_missing=True)
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


# --- L0/L1 Summary generation ---

async def _generate_summary_async(content: str) -> tuple[str, str]:
    """Async helper that calls claude-code-sdk to generate l0/l1 summaries."""
    prompt = (
        'Return JSON only: {"l0": "<one sentence ≤25 words>", "l1": "<2-3 sentences 60-100 words>"}\n\n'
        f"Note content:\n{content[:2000]}"
    )
    try:
        from claude_code_sdk import query, ClaudeCodeOptions, AssistantMessage
        full_text = ""
        async for message in query(prompt=prompt, options=ClaudeCodeOptions(allowed_tools=[])):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text"):
                        full_text += block.text
        # Non-greedy match to get the first complete JSON object
        for m in re.finditer(r'\{[^{}]*\}', full_text, re.DOTALL):
            try:
                data = json.loads(m.group())
                if "l0" in data or "l1" in data:
                    return str(data.get("l0", "") or ""), str(data.get("l1", "") or "")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return "", ""


def _generate_summary(content: str) -> tuple[str, str]:
    """Sync wrapper for summary generation. Returns ('', '') on any failure."""
    try:
        return asyncio.run(_generate_summary_async(content))
    except Exception:
        return "", ""


def _inject_summary_into_frontmatter(text: str, l0: str, l1: str) -> str:
    """Inject l0, l1, l1_generated into existing frontmatter. Returns updated text."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return text
    today_str = date.today().isoformat()
    if l0:
        fm["l0"] = l0
    if l1:
        fm["l1"] = l1
    if l0 or l1:
        fm["l1_generated"] = today_str
    new_fm = "---\n" + yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip() + "\n---"
    return new_fm + text[end + 4:]


# --- MOC helpers ---

def _is_moc(path: Path) -> bool:
    """Return True if note is a Map of Content."""
    moc_keywords = {"moc", "index", "overview", "hub"}
    stem_lower = path.stem.lower()
    if any(re.search(r'\b' + kw + r'\b', stem_lower) for kw in moc_keywords):
        return True
    fm = _parse_frontmatter(path) or {}
    if str(fm.get("type", "")).lower() == "moc":
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    wikilinks = re.findall(r'\[\[([^\]|]+)', text)
    if len(wikilinks) >= 5:
        targets = []
        for stem in wikilinks:
            stem = stem.strip()
            found = _resolve_wikilink(stem)
            if found:
                targets.append(str(found.parent))
        if targets and len(set(targets)) == 1 and set(targets) != {str(path.parent)}:
            return True
    return False


def _build_moc_map() -> dict[str, Path]:
    """Scan vault for MOC notes. Returns {folder_path_str: moc_note_path}."""
    result: dict[str, Path] = {}
    for md_file in VAULT_PATH.rglob("*.md"):
        if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
            continue
        if _is_moc(md_file):
            folder_str = str(md_file.parent)
            result[folder_str] = md_file
    return result


# --- Graph walk helpers ---

def _parse_wikilinks(content: str) -> list[str]:
    """Extract [[target]] and [[target|alias]] stems from note body."""
    raw = re.findall(r'\[\[([^\]]+)\]\]', content)
    stems = []
    for r in raw:
        stem = r.split("|")[0].strip()
        stem = re.sub(r'\.md$', '', stem, flags=re.IGNORECASE)
        if stem:
            stems.append(stem)
    return stems


def _resolve_wikilink(stem: str) -> Path | None:
    """Find .md file by stem in vault (case-insensitive). Returns None if not found."""
    stem_lower = stem.lower()
    for md_file in VAULT_PATH.rglob("*.md"):
        if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
            continue
        if md_file.stem.lower() == stem_lower:
            return md_file
    return None


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
def obsidian_read_note(
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    summary_only: bool = False,
) -> dict:
    """Read a vault note, optionally by line range or summary-only mode."""
    p = vault_path(path)
    if not p.exists():
        raise ToolError(f"Note not found: {path}")
    if summary_only:
        fm = _parse_frontmatter(p) or {}
        total = len(p.read_text(encoding="utf-8").splitlines())
        return {
            K_PATH: path,
            K_L0: str(fm.get("l0", "") or ""),
            "l1": str(fm.get("l1", "") or ""),
            K_LINES: total,
        }
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
def obsidian_write_note(
    path: str,
    content: str,
    overwrite: bool = False,
    l0: str = "",
    l1: str = "",
) -> dict:
    """Create or overwrite a vault note. Provide l0 (≤25-word summary) and l1 (2-3 sentence overview) for notes with meaningful content — written to frontmatter for tiered retrieval."""
    p = vault_path(path)
    # Auto-generate summaries if content has frontmatter, body >200 chars, and l0 not provided
    if content.startswith("---\n") and not l0:
        fm_end = content.find("\n---", 4)
        body = content[fm_end + 4:].strip() if fm_end != -1 else ""
        if len(body) > 200:
            l0, l1 = _generate_summary(content)
    if l0 or l1:
        content = _inject_summary_into_frontmatter(content, l0, l1)
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
def obsidian_append_to_note(
    path: str,
    content: str,
    add_separator: bool = False,
    before_section: str | None = None,
) -> dict:
    """Append content to a vault note, creating it if absent. before_section inserts before the matching heading."""
    p = vault_path(path)
    if before_section and p.exists():
        text = p.read_text(encoding="utf-8")
        heading_re = re.compile(r'^(#{1,6})\s+(.*?)\s*$', re.MULTILINE)
        lines = text.splitlines(keepends=True)
        insert_idx = None
        for i, line in enumerate(lines):
            m = heading_re.match(line.rstrip("\n").rstrip("\r"))
            if m and m.group(2).lower() == before_section.lower():
                insert_idx = i
                break
        if insert_idx is not None:
            sep = "\n---\n\n" if add_separator else "\n"
            insert_content = content if content.endswith("\n") else content + "\n"
            new_text = "".join(lines[:insert_idx]) + insert_content + sep + "".join(lines[insert_idx:])
            p.write_text(new_text, encoding="utf-8")
            return {K_PATH: path, "appended_bytes": len(content.encode()), "inserted_before": before_section}
    _append_note(p, content, add_separator)
    return {K_PATH: path, "appended_bytes": len(content.encode())}


@mcp.tool()
def obsidian_move_note(from_path: str, to_path: str) -> dict:
    """Move or rename a single note."""
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
def obsidian_list_folder(
    folder: str = "",
    include_preview: bool = False,
    recursive: bool = False,
    names_only: bool = False,
) -> dict:
    """List notes and subfolders. names_only=True returns a flat list of relative paths."""
    p = vault_path(folder) if folder else VAULT_PATH
    if not p.is_dir():
        raise ToolError(f"Folder not found: {folder}")
    if names_only:
        paths = []
        for md_file in sorted(p.rglob("*.md") if recursive else p.glob("*.md")):
            if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
                continue
            paths.append(str(md_file.relative_to(VAULT_PATH)))
        return {"folder": folder or "/", "paths": paths}
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
                    entry["v"] = text[:SEARCH_PREVIEW_LEN].replace("\n", " ")
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
                        entry["v"] = text[:SEARCH_PREVIEW_LEN].replace("\n", " ")
                    except (OSError, UnicodeDecodeError):
                        pass
                items.append(entry)
    return {"folder": folder or "/", "items": items}


def _save_chat_core(
    title: str,
    summary: str,
    content: str,
    tags: list[str] | None = None,
    project: str = "",
    folder: str = "",
    l0: str = "",
    l1: str = "",
    custom_date: str | None = None,
) -> dict:
    tags = tags or []
    if custom_date:
        try:
            today = date.fromisoformat(custom_date)
        except ValueError:
            raise ToolError(f"custom_date must be YYYY-MM-DD, got: {custom_date!r}")
    else:
        today = date.today()
    target_folder = folder or CHATS_FOLDER
    filename = f"{today.strftime('%Y-%m-%d')} {title}.md"
    p = vault_path(f"{target_folder}/{filename}")
    p.parent.mkdir(parents=True, exist_ok=True)

    if not l0 or not l1:
        gen_l0, gen_l1 = _generate_summary(content)
        if not l0:
            l0 = gen_l0
        if not l1:
            l1 = gen_l1
    missing_summaries = not l0 and not l1

    all_tags = ["claude", "chat"] + tags
    fm_data: dict = {
        "date": today.strftime('%Y-%m-%d'),
        "tags": all_tags,
        "summary": summary,
        "source": "claude-desktop",
    }
    if project:
        fm_data["project"] = project
    if l0:
        fm_data["l0"] = l0
    if l1:
        fm_data["l1"] = l1
    if l0 or l1:
        fm_data["l1_generated"] = today.isoformat()
    frontmatter = "---\n" + yaml.dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip() + "\n---"

    if p.exists():
        now = datetime.now().strftime("%H:%M")
        section = f"\n\n## {now} — {title}\n\n{content}"
        existing = p.read_text(encoding="utf-8")
        p.write_text(existing + section, encoding="utf-8")
        result: dict = {"p": str(p.relative_to(VAULT_PATH)), "appended": True}
    else:
        full = f"{frontmatter}\n\n# {title}\n\n{content}"
        p.write_text(full, encoding="utf-8")
        result = {"p": str(p.relative_to(VAULT_PATH)), "appended": False}
    if missing_summaries:
        result["warn"] = "l0/l1 not written — provide them explicitly for tiered retrieval"
    return result


@mcp.tool()
def obsidian_save_chat(
    title: str,
    summary: str,
    content: str,
    tags: list[str] | None = None,
    project: str = "",
    folder: str = "",
    l0: str = "",
    l1: str = "",
    custom_date: str | None = None,
    mode: Literal["normal", "condensed", "ultra"] = "normal",
) -> dict:
    """Save a Claude conversation to Obsidian. Always provide l0 (≤25-word one-sentence summary) and l1 (2-3 sentence ~80-word overview) — written to frontmatter for tiered retrieval. custom_date: YYYY-MM-DD to override today's date.

    mode controls how you should format the `content` field:
    - "normal" (default): no format imposed — use your best judgment
    - "condensed": bullets only — ## Summary (context + what + outcome) and ## Takeaways (insights, gotchas, small-but-important details, next steps); omit ## Takeaways only if truly nothing notable
    - "ultra": single YAML block, machine-readable only, no prose — keys: topic, ctx, did, out, files, insights, next; include all applicable keys
    """
    return _save_chat_core(title, summary, content, tags, project, folder, l0, l1, custom_date)


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
    content_max_chars: int = 500,
    modified_after: str | None = None,
    modified_before: str | None = None,
    tags: list[str] | None = None,
    tier: Literal["l0", "l1", "full"] = "full",
) -> dict:
    """Full-text search across vault notes. tier='l0'/'l1' returns only that frontmatter summary field."""
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
            if tier in ("l0", "l1"):
                fm = _parse_frontmatter(md_file) or {}
                entry[tier] = str(fm.get(tier, "") or "")
            elif include_content:
                entry["content"] = text[:content_max_chars]
            results.append(entry)
        else:
            for m in file_matches:
                results.append({"p": rel, "l": m["l"], "v": m["v"]})

    return {"query": query, "files" if group_by_file else "matches": results, "truncated": truncated, "scanned": scanned}


@mcp.tool()
def obsidian_batch(operations: list[dict], confirm: bool = False) -> dict:
    """Preferred for 2+ write/move/delete/append ops — one tool call."""
    VALID_OPS = {"write", "move", "delete", "append", "find_related", "patch_section", "save_chat"}
    for i, op in enumerate(operations):
        op_type = op.get("op")
        if op_type not in VALID_OPS:
            raise ToolError(f"Operation {i}: invalid op '{op_type}'. Must be one of {VALID_OPS}")
        if op_type != "save_chat":
            path = op.get("path")
            if not isinstance(path, str) or not path:
                raise ToolError(f"Operation {i}: 'path' must be a non-empty string")
        if op_type == "move":
            to = op.get("to")
            if not isinstance(to, str) or not to:
                raise ToolError(f"Operation {i}: 'to' must be a non-empty string for move")
        if op_type == "save_chat":
            for req in ("title", "summary", "content"):
                if not op.get(req):
                    raise ToolError(f"Operation {i}: save_chat requires '{req}'")

    def _do_write(op):
        p = vault_path(op["path"])
        created = _write_note(p, op.get("content") or "", op.get("overwrite", False))
        return {"index": op["_i"], "p": op["path"], "ok": True, "created": created}

    def _do_append(op):
        p = vault_path(op["path"])
        _append_note(p, op.get("content") or "", op.get("add_separator", False))
        return {"index": op["_i"], "p": op["path"], "ok": True}

    def _do_move(op):
        src = vault_path(op["path"])
        dst = vault_path(op["to"])
        _move_note(src, dst)
        return {"index": op["_i"], "from": op["path"], "to": op["to"], "ok": True}

    def _do_delete(op):
        if not confirm and not op.get("confirm"):
            raise ToolError("delete requires confirm=true on the batch or the individual op")
        p = vault_path(op["path"])
        _delete_note(p)
        return {"index": op["_i"], "p": op["path"], "ok": True}

    def _do_patch_section(op):
        p = vault_path(op["path"])
        match_type = op.get("match_type", "heading")
        if match_type not in ("heading", "text", "section"):
            raise ToolError(f"Operation {op['_i']}: match_type must be 'heading', 'text', or 'section'")
        status = _patch_section(
            p,
            op["match"],
            match_type,
            op.get("content", ""),
            heading_level=op.get("heading_level"),
            create_if_missing=op.get("create_if_missing", True),
        )
        return {"index": op["_i"], "p": op["path"], "ok": True, "status": status}

    def _do_find_related(op):
        try:
            fr = _find_related_core(
                op["path"],
                top_k=op.get("max_results", 10),
                folder=op.get("folder", ""),
                min_score=op.get("min_score", FIND_RELATED_MIN_SCORE),
            )
            return {"index": op["_i"], "p": op["path"], "ok": True, "related": fr["related"]}
        except Exception as e:
            return {"index": op["_i"], "p": op["path"], "ok": False, "error": str(e)}

    def _do_save_chat(op):
        result = _save_chat_core(
            title=op["title"],
            summary=op["summary"],
            content=op["content"],
            tags=op.get("tags"),
            project=op.get("project", ""),
            folder=op.get("folder", ""),
            l0=op.get("l0", ""),
            l1=op.get("l1", ""),
            custom_date=op.get("custom_date"),
        )
        return {"index": op["_i"], "ok": True, **result}

    OPS = {
        "write": _do_write,
        "append": _do_append,
        "move": _do_move,
        "delete": _do_delete,
        "patch_section": _do_patch_section,
        "find_related": _do_find_related,
        "save_chat": _do_save_chat,
    }

    results = []
    for i, op in enumerate(operations):
        op_with_idx = {**op, "_i": i}
        try:
            result = OPS[op["op"]](op_with_idx)
            results.append(result)
        except ToolError:
            raise
        except Exception as e:
            results.append({"index": i, "ok": False, "error": str(e)})

    success = sum(1 for r in results if r.get("ok"))
    return {"results": results, "success_count": success, "error_count": len(results) - success}


@mcp.tool()
def obsidian_patch_frontmatter(path: str, updates: dict, remove_keys: list[str] | None = None) -> dict:
    """Merge updates into a note's YAML frontmatter without touching the body."""
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
    """Move an entire folder (all notes and subfolders) to a new location."""
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
    min_score: float = FIND_RELATED_MIN_SCORE,
) -> dict:
    """Discover notes topically related to a given note by shared tags, title words, and body content."""
    core = _find_related_core(path, top_k=top_k, folder=folder, min_score=min_score)
    trimmed = [
        {K_PATH: r[K_PATH], K_SCORE: r["score"], K_TAGS: r["shared_tags"], K_L0: r[K_L0]}
        for r in core["related"]
    ]
    return {"source": path, "related": trimmed}


@mcp.tool()
def obsidian_patch_section(
    path: str,
    match: str,
    match_type: Literal["heading", "text", "section"],
    content: str = "",
    heading_level: int | None = None,
    create_if_missing: bool = True,
) -> dict:
    """Surgical content editor: replace heading body, delete section, or find-and-replace text."""
    p = vault_path(path)
    status = _patch_section(p, match, match_type, content, heading_level=heading_level, create_if_missing=create_if_missing)
    return {"p": path, "status": status}


@mcp.tool()
def obsidian_backfill_summaries(
    folder: str = "",
    limit: int = 50,
    overwrite_stale_days: int = 90,
) -> dict:
    """Backfill l0/l1 summaries for notes missing them or with stale l1_generated."""
    root = vault_path(folder) if folder else VAULT_PATH
    if not root.is_dir():
        raise ToolError(f"Folder not found: {folder}")

    processed = 0
    skipped = 0
    errors: list[str] = []
    today = date.today()

    for md_file in sorted(root.rglob("*.md")):
        if processed >= limit:
            break
        if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
            continue
        try:
            fm = _parse_frontmatter(md_file) or {}
            has_l0 = bool(fm.get("l0"))
            l1_generated_str = str(fm.get("l1_generated", "") or "")
            stale = False
            if l1_generated_str:
                try:
                    gen_date = date.fromisoformat(l1_generated_str)
                    stale = (today - gen_date).days > overwrite_stale_days
                except ValueError:
                    stale = True
            if has_l0 and not stale:
                skipped += 1
                continue
            text = md_file.read_text(encoding="utf-8")
            l0, l1 = _generate_summary(text)
            if not l0:
                errors.append(f"{md_file.relative_to(VAULT_PATH)}: summary generation returned empty")
                continue
            updates: dict = {"l1_generated": today.isoformat()}
            if l0:
                updates["l0"] = l0
            if l1:
                updates["l1"] = l1
            obsidian_patch_frontmatter(str(md_file.relative_to(VAULT_PATH)), updates)
            processed += 1
        except Exception as e:
            errors.append(f"{md_file.relative_to(VAULT_PATH)}: {e}")

    return {"processed": processed, "skipped": skipped, "errors": errors}


@mcp.tool()
def obsidian_graph_walk(
    path: str,
    depth: int = 2,
    direction: Literal["out", "in", "both"] = "both",
    include_l0: bool = True,
) -> dict:
    """BFS traversal of note links up to depth hops. direction='out' follows wikilinks, 'in' finds backlinks, 'both' unions them."""
    source_path = vault_path(path)
    if not source_path.exists():
        raise ToolError(f"Note not found: {path}")

    depth = max(1, min(depth, 6))
    source_rel = str(source_path.relative_to(VAULT_PATH))

    # Build reverse index if needed for 'in' or 'both'
    reverse_index: dict[str, set[str]] = {}
    if direction in ("in", "both"):
        for md_file in VAULT_PATH.rglob("*.md"):
            if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for stem in _parse_wikilinks(text):
                target = _resolve_wikilink(stem)
                if target:
                    target_rel = str(target.relative_to(VAULT_PATH))
                    reverse_index.setdefault(target_rel, set()).add(
                        str(md_file.relative_to(VAULT_PATH))
                    )

    nodes: dict[str, dict] = {}
    queue: list[tuple[str, int]] = [(source_rel, 0)]
    visited: set[str] = {source_rel}

    while queue:
        current_rel, current_depth = queue.pop(0)
        if current_depth >= depth:
            continue
        current_path = vault_path(current_rel)
        neighbors: dict[str, str] = {}  # rel_path -> direction

        if direction in ("out", "both"):
            try:
                text = current_path.read_text(encoding="utf-8")
                for stem in _parse_wikilinks(text):
                    resolved = _resolve_wikilink(stem)
                    if resolved:
                        rel = str(resolved.relative_to(VAULT_PATH))
                        if rel != source_rel:
                            neighbors[rel] = "out"
            except (OSError, UnicodeDecodeError):
                pass

        if direction in ("in", "both"):
            for back_rel in reverse_index.get(current_rel, set()):
                if back_rel != source_rel:
                    if back_rel in neighbors:
                        neighbors[back_rel] = "both"
                    else:
                        neighbors[back_rel] = "in"

        for neighbor_rel, link_dir in neighbors.items():
            node_degree = current_depth + 1
            if neighbor_rel in nodes:
                existing = nodes[neighbor_rel]
                if existing["dir"] != link_dir:
                    nodes[neighbor_rel]["dir"] = "both"
                continue
            l0_val = ""
            if include_l0:
                try:
                    fm = _parse_frontmatter(vault_path(neighbor_rel)) or {}
                    l0_val = str(fm.get("l0", "") or "")
                except Exception:
                    pass
            nodes[neighbor_rel] = {"degree": node_degree, "dir": link_dir, "l0": l0_val}
            if neighbor_rel not in visited:
                visited.add(neighbor_rel)
                queue.append((neighbor_rel, node_degree))

    return {"source": source_rel, "nodes": nodes}


@mcp.tool()
def obsidian_relink(
    mode: Literal["normal", "extended", "full", "undo", "orphan"] = "normal",
    min_score: float = RELINK_MIN_SCORE,
    exclude_folders: list[str] | None = None,
    smart: bool = False,
) -> dict:
    """Automate building and maintaining ## Related sections across notes. mode='extended' links the most recent chat note to the full vault with MOC-aware suppression (like full, but single note only)."""
    undo_file = VAULT_PATH / ".relink-undo.json"
    excluded = [f.replace("\\", "/").rstrip("/") for f in (exclude_folders or [])]

    def _is_excluded(rel_path: str) -> bool:
        fwd = rel_path.replace("\\", "/")
        return any(fwd == ex or fwd.startswith(ex + "/") for ex in excluded)

    if mode == "undo":
        if not undo_file.exists():
            return {"mode": "undo", "status": "no_backup_found"}
        stack = json.loads(undo_file.read_text(encoding="utf-8"))
        if isinstance(stack, dict):  # migrate old single-entry format
            stack = [stack]
        if not stack:
            undo_file.unlink(missing_ok=True)
            return {"mode": "undo", "status": "no_backup_found"}
        backup = stack.pop(0)
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
                                   heading_level=None, create_if_missing=True)
                else:
                    _remove_section(p, "Related", heading_level=None)
                restored += 1
            except Exception:
                skipped += 1
        if stack:
            undo_file.write_text(json.dumps(stack, indent=2), encoding="utf-8")
        else:
            undo_file.unlink(missing_ok=True)
        return {"mode": "undo", "restored": restored, "skipped": skipped,
                "from_timestamp": backup.get("timestamp"),
                "undos_remaining": len(stack)}

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
        heuristic_min = FIND_RELATED_MIN_SCORE if smart else min_score
        result = _find_related_core(target_str, top_k=20 if smart else 10, min_score=heuristic_min)
        related = [
            r for r in result["related"]
            if not _is_excluded(r[K_PATH])
            and _passes_tag_filter(r["shared_tags"], target.stem, Path(r[K_PATH]).stem)
        ]
        related = _cap_same_folder_links(related, target.parent)
        if smart:
            return {
                "mode": "normal",
                "smart": True,
                "status": "review_pending",
                "target": target_str,
                "target_content": _read_full_content(target),
                "candidates": [
                    {**r, "content": _read_full_content(VAULT_PATH / r[K_PATH])}
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
        stack = json.loads(undo_file.read_text(encoding="utf-8")) if undo_file.exists() else []
        if isinstance(stack, dict):  # migrate old single-entry format
            stack = [stack]
        stack.insert(0, {"timestamp": datetime.now().isoformat(), "mode": "normal",
                         "entries": [_capture_related_state(target)]})
        undo_file.write_text(json.dumps(stack[:5], indent=2), encoding="utf-8")
        status = _apply_relink(target, entries)
        return {"mode": "normal", "note": target_str, "status": status, "links_considered": len(entries)}

    if mode == "extended":
        if not chats_path.is_dir():
            raise ToolError(f"Chats folder not found: {CHATS_FOLDER}")
        notes = [
            f for f in chats_path.rglob("*.md")
            if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
        ]
        if not notes:
            return {"mode": "extended", "note": None, "status": "no_notes_found"}
        target = max(notes, key=lambda f: f.stat().st_mtime)
        target_str = str(target.relative_to(VAULT_PATH))

        # Skip if the target itself is a MOC note
        moc_map = _build_moc_map()
        moc_note_paths = set(moc_map.values())
        if target in moc_note_paths:
            return {"mode": "extended", "note": target_str, "status": "skipped_moc"}

        # Pre-load MOC wikilinks for intra-group suppression
        moc_wikilinks: dict[str, set[str]] = {}
        for folder_str, moc_path in moc_map.items():
            try:
                moc_text = moc_path.read_text(encoding="utf-8")
                moc_wikilinks[folder_str] = set(_parse_wikilinks(moc_text))
            except (OSError, UnicodeDecodeError):
                moc_wikilinks[folder_str] = set()

        heuristic_min = FIND_RELATED_MIN_SCORE if smart else min_score
        result = _find_related_core(target_str, top_k=20 if smart else 10, min_score=heuristic_min)
        related = []
        for r in result["related"]:
            if _is_excluded(r[K_PATH]):
                continue
            if not _passes_tag_filter(r["shared_tags"], target.stem, Path(r[K_PATH]).stem):
                continue
            candidate_path = VAULT_PATH / r[K_PATH]
            # MOC intra-group suppression
            if candidate_path.parent == target.parent:
                folder_str = str(target.parent)
                if folder_str in moc_map:
                    moc_links = moc_wikilinks.get(folder_str, set())
                    if target.stem in moc_links and candidate_path.stem in moc_links:
                        continue
            related.append(r)
        related = _cap_same_folder_links(related, target.parent)

        if smart:
            return {
                "mode": "extended",
                "smart": True,
                "status": "review_pending",
                "target": target_str,
                "target_content": _read_full_content(target),
                "candidates": [
                    {**r, "content": _read_full_content(VAULT_PATH / r[K_PATH])}
                    for r in related
                ],
                "instruction": (
                    "Review the target note and each candidate's full content. "
                    "Select only candidates that are GENUINELY related. "
                    "Then call obsidian_patch_section with path=target, match='Related', match_type='heading' "
                    "and content as a bullet list of [[wikilinks]] for verified candidates."
                ),
            }

        entries = [_format_related_entry(r["title"], r["shared_tags"]) for r in related]
        if not entries:
            return {"mode": "extended", "note": target_str, "status": "no_matches"}
        stack = json.loads(undo_file.read_text(encoding="utf-8")) if undo_file.exists() else []
        if isinstance(stack, dict):  # migrate old single-entry format
            stack = [stack]
        stack.insert(0, {"timestamp": datetime.now().isoformat(), "mode": "extended",
                         "entries": [_capture_related_state(target)]})
        undo_file.write_text(json.dumps(stack[:5], indent=2), encoding="utf-8")
        status = _apply_relink(target, entries)
        return {"mode": "extended", "note": target_str, "status": status, "links_considered": len(entries)}

    if mode == "orphan":
        if not chats_path.is_dir():
            raise ToolError(f"Chats folder not found: {CHATS_FOLDER}")
        chat_notes = [
            f for f in chats_path.rglob("*.md")
            if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
            and not _is_excluded(str(f.relative_to(VAULT_PATH)))
        ]
        if not chat_notes:
            return {"mode": "orphan", "status": "no_notes_found", "orphans": []}

        # Build reverse index: stem → set of files that link to it
        reverse_index: dict[str, set[str]] = {}
        for md_file in VAULT_PATH.rglob("*.md"):
            if any(part.startswith(".") for part in md_file.relative_to(VAULT_PATH).parts):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for stem in _parse_wikilinks(text):
                reverse_index.setdefault(stem.lower(), set()).add(str(md_file.relative_to(VAULT_PATH)))

        # Find orphans: no outgoing links AND no backlinks
        orphans = []
        for note_file in chat_notes:
            try:
                content = note_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            has_outgoing = bool(_parse_wikilinks(content))
            has_backlinks = bool(reverse_index.get(note_file.stem.lower()))
            if not has_outgoing and not has_backlinks:
                orphans.append(note_file)

        if not orphans:
            return {"mode": "orphan", "status": "no_orphans", "orphans": []}

        # MOC setup (same as extended)
        moc_map = _build_moc_map()
        moc_note_paths = set(moc_map.values())
        moc_wikilinks: dict[str, set[str]] = {}
        for folder_str, moc_path in moc_map.items():
            try:
                moc_text = moc_path.read_text(encoding="utf-8")
                moc_wikilinks[folder_str] = set(_parse_wikilinks(moc_text))
            except (OSError, UnicodeDecodeError):
                moc_wikilinks[folder_str] = set()

        # Snapshot all orphans for undo before touching any
        backup_entries = [_capture_related_state(f) for f in orphans]
        stack = json.loads(undo_file.read_text(encoding="utf-8")) if undo_file.exists() else []
        if isinstance(stack, dict):  # migrate old single-entry format
            stack = [stack]
        stack.insert(0, {"timestamp": datetime.now().isoformat(), "mode": "orphan",
                         "entries": backup_entries})
        undo_file.write_text(json.dumps(stack[:5], indent=2), encoding="utf-8")

        updated = 0
        no_matches = 0
        errors: list[dict] = []
        processed = []
        for note_file in orphans:
            if note_file in moc_note_paths:
                continue
            note_str = str(note_file.relative_to(VAULT_PATH))
            try:
                result = _find_related_core(note_str, top_k=10, min_score=min_score)
            except Exception as e:
                errors.append({"p": note_str, "error": str(e)})
                continue
            related = []
            for r in result["related"]:
                if _is_excluded(r[K_PATH]):
                    continue
                if not _passes_tag_filter(r["shared_tags"], note_file.stem, Path(r[K_PATH]).stem):
                    continue
                candidate_path = VAULT_PATH / r[K_PATH]
                if candidate_path.parent == note_file.parent:
                    folder_str = str(note_file.parent)
                    if folder_str in moc_map:
                        moc_links = moc_wikilinks.get(folder_str, set())
                        if note_file.stem in moc_links and candidate_path.stem in moc_links:
                            continue
                related.append(r)
            related = _cap_same_folder_links(related, note_file.parent)
            entries = [_format_related_entry(r["title"], r["shared_tags"]) for r in related]
            if not entries:
                no_matches += 1
                processed.append({"p": note_str, "status": "no_matches"})
                continue
            status = _apply_relink(note_file, entries)
            if status == "updated":
                updated += 1
            processed.append({"p": note_str, "status": status})

        return {
            "mode": "orphan",
            "orphans_found": len(orphans),
            "updated": updated,
            "no_matches": no_matches,
            "errors": errors,
            "processed": processed,
        }

    # mode == "full"
    if smart:
        raise ToolError("smart=True is only supported with mode='normal' or mode='extended'")

    all_notes = [
        f for f in VAULT_PATH.rglob("*.md")
        if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)
        and not _is_excluded(str(f.relative_to(VAULT_PATH)))
    ]

    # Pre-compute vault stems and MOC data for the full scan
    vault_stems = {f.stem for f in VAULT_PATH.rglob("*.md")
                   if not any(part.startswith(".") for part in f.relative_to(VAULT_PATH).parts)}
    moc_map = _build_moc_map()
    moc_note_paths = set(moc_map.values())

    # Pre-load MOC wikilinks for intra-group suppression
    moc_wikilinks: dict[str, set[str]] = {}
    for folder_str, moc_path in moc_map.items():
        try:
            moc_text = moc_path.read_text(encoding="utf-8")
            moc_wikilinks[folder_str] = set(_parse_wikilinks(moc_text))
        except (OSError, UnicodeDecodeError):
            moc_wikilinks[folder_str] = set()

    forward: dict[str, list[tuple[str, list[str]]]] = {}
    errors: list[dict] = []
    no_matches = 0

    for note_file in all_notes:
        note_str = str(note_file.relative_to(VAULT_PATH))
        # Skip MOC notes as link targets
        if note_file in moc_note_paths:
            no_matches += 1
            continue
        try:
            result = _find_related_core(note_str, top_k=10, min_score=min_score)
        except Exception as e:
            errors.append({"p": note_str, "error": str(e)})
            continue
        related = []
        for r in result["related"]:
            if _is_excluded(r[K_PATH]):
                continue
            if not _passes_tag_filter(r["shared_tags"], note_file.stem, Path(r[K_PATH]).stem):
                continue
            candidate_path = VAULT_PATH / r[K_PATH]
            # MOC-aware intra-group suppression
            if candidate_path.parent == note_file.parent:
                folder_str = str(note_file.parent)
                if folder_str in moc_map:
                    moc_links = moc_wikilinks.get(folder_str, set())
                    if note_file.stem in moc_links and candidate_path.stem in moc_links:
                        continue
            related.append(r)
        related = _cap_same_folder_links(related, note_file.parent)
        filtered = [(r["title"], r["shared_tags"]) for r in related]
        if filtered:
            forward[note_str] = filtered
        else:
            no_matches += 1

    # Deduplicate: for each pair (A, B), keep only the first direction encountered.
    # Prevents symmetric scoring from creating A→B and B→A in the same pass,
    # which would artificially inflate node degree (fat nodes).
    seen_pairs: set[frozenset] = set()
    deduped: dict[str, list[tuple[str, list[str]]]] = {}
    for note_str, entries in forward.items():
        kept = []
        for title, tags in entries:
            pair: frozenset = frozenset({Path(note_str).stem, title})
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                kept.append((title, tags))
        if kept:
            deduped[note_str] = kept
        else:
            no_matches += 1
    forward = deduped

    backup_entries = [_capture_related_state(vault_path(ns)) for ns in forward]
    stack = json.loads(undo_file.read_text(encoding="utf-8")) if undo_file.exists() else []
    stack.insert(0, {"timestamp": datetime.now().isoformat(), "mode": "full", "entries": backup_entries})
    undo_file.write_text(json.dumps(stack[:5], indent=2), encoding="utf-8")

    updated = 0
    no_change = 0
    for note_str, entries in forward.items():
        try:
            note_file = vault_path(note_str)
            formatted = [_format_related_entry(title, tags) for title, tags in entries]
            status = _apply_relink(note_file, formatted, vault_stems=vault_stems)
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


@mcp.tool()
def obsidian_help(topic: str = "") -> dict:
    """Return usage guide for Myobscelium tools. topic: tool name or category to filter (empty = full guide)."""
    guide = {
        "overview": (
            "Myobscelium — 19 tools organized into 6 categories. "
            "All paths are relative to the vault root. "
            "Responses use abbreviated keys: p=path, c=content, m=modified, lc=line_count, s=score, st=shared_tags, l0=l0_summary."
        ),
        "categories": {
            "READ": {
                "tools": ["obsidian_vault_overview", "obsidian_read_note", "obsidian_read_frontmatter", "obsidian_list_folder"],
                "when": "Exploring structure or loading note content.",
                "tips": [
                    "vault_overview: start of a session to orient — use mode='compact' (default) to save tokens, mode='tree' for visual layout.",
                    "read_note: use summary_only=True to get only l0+l1+line_count without loading the body — good for deciding whether to load full.",
                    "read_frontmatter: cheapest way to check tags, project, l0/l1 without any body content.",
                    "list_folder: use names_only=True for a flat path list when you just need to know what exists; include_preview=True when you want a snippet of each note.",
                ],
            },
            "WRITE": {
                "tools": ["obsidian_write_note", "obsidian_append_to_note", "obsidian_save_chat", "obsidian_patch_frontmatter", "obsidian_patch_section"],
                "when": "Creating or modifying notes.",
                "tips": [
                    "write_note: full create/overwrite. Always provide l0+l1 for meaningful notes — they go to frontmatter for tiered retrieval.",
                    "append_to_note: add content to end of a note. Use before_section='## Related' to insert BEFORE the Related section instead of after it (avoids breaking link structure).",
                    "save_chat: saves a Claude conversation. ALWAYS provide l0 (<=25-word summary) and l1 (2-3 sentence overview) as params — auto-generation is unreliable. Use custom_date='YYYY-MM-DD' for saving old chats. Response includes warn key if l0/l1 were missing.",
                    "patch_frontmatter: surgical YAML field updates without touching the note body — use for adding tags, updating project, setting custom fields.",
                    "patch_section: surgical body edits. match_type='heading' replaces a section under a heading; match_type='text' finds and replaces a string; match_type='section' deletes an entire section. Preferred over write_note for partial edits.",
                ],
            },
            "ORGANIZE": {
                "tools": ["obsidian_move_note", "obsidian_delete_note", "obsidian_move_folder", "obsidian_batch"],
                "when": "Restructuring the vault.",
                "tips": [
                    "move_note: also works as rename — same folder, different filename.",
                    "delete_note: requires confirm=True. Irreversible.",
                    "move_folder: moves entire subtree. Use with care — wikilinks to moved notes will break.",
                    "batch: preferred for 2+ operations in one call. Supports ops: write, append, move, delete, patch_section, find_related, save_chat. Each op is a dict with 'op' key plus op-specific fields. delete requires confirm=True on the batch or the individual op.",
                ],
            },
            "SEARCH": {
                "tools": ["obsidian_search", "obsidian_find_related"],
                "when": "Finding notes by content or similarity.",
                "tips": [
                    "search: text/regex search across the vault. Use tier='l0' or tier='l1' to return only frontmatter summaries for matches — much cheaper than loading full content. content_max_chars defaults to 500.",
                    "find_related: IDF scoring across tags + title words + body words. Returns abbreviated keys (p, s, st, l0). Use to discover connections you didn't know existed. Same-folder notes are dampened by 0.4x to reduce project-folder noise.",
                ],
            },
            "GRAPH": {
                "tools": ["obsidian_graph_walk", "obsidian_relink", "obsidian_backfill_summaries"],
                "when": "Working with the vault's link graph.",
                "tips": [
                    "graph_walk: traverses EXISTING [[wikilinks]] outward from a note. Different from find_related — graph_walk follows links you already made, find_related discovers new connections via scoring. Use direction='both' to see what links to+from a note; direction='out' for what a note points to; direction='in' for backlinks. include_l0=True adds one-line summaries to each discovered node. Great for building context: graph_walk from the most recent note → degree-1 = primary context, degree-2 = secondary.",
                    "relink: auto-populates ## Related sections with scored links. mode='normal' relinks the most recent chat note (Claude/Chats only); mode='extended' relinks the most recent chat note against the full vault with MOC-aware suppression — use after saving a chat to connect it to broader notes; mode='orphan' finds ALL chat notes with zero outgoing links AND zero backlinks and relinks them vault-wide — use to catch forgotten chats without running full; mode='full' scans and relinks every note in the vault (slow, use sparingly); mode='undo' reverts the last relink (up to 5 deep). smart=True returns candidates for human review without writing. MOC-aware: notes in a folder with a Map of Content won't get redundant intra-group links. Cross-project: notes with different project: frontmatter (or inferred from Projects/<Name>/ folder) are dampened 0.3× so sharing a project-specific tag like #mcp across Myobscelium and Meridian Sage won't drive a link. Same-folder links capped at 1 (prevents session blob clusters).",
                    "backfill_summaries: generates l0/l1 for notes that are missing them. Calls Claude via claude_code_sdk — may fail silently if SDK can't nest sessions. Use limit param to avoid runaway calls. Errors appear in the errors[] list.",
                ],
            },
        },
        "tier_system": {
            "l0": "1-sentence frontmatter field (~25 words). Used by find_related for cheap scanning. Read via read_frontmatter or summary_only=True.",
            "l1": "2-3 sentence frontmatter field (~80 words). The human-readable orientation layer. Read via read_frontmatter or summary_only=True.",
            "l2": "The full note body. Only loaded when you actually call read_note without summary_only. Dense machine notation is preferred over prose for chat saves — higher keyword density = better retrieval scores.",
            "when_to_use_each": "Start with l0 (scan many notes cheaply) → escalate to l1 (understand the note) → escalate to l2 (full content) only for notes that are definitely relevant.",
        },
        "common_workflows": {
            "start_of_session": "vault_overview → graph_walk from most recent relevant note (depth=2, include_l0=True) → read_note(summary_only=True) on interesting neighbors → read_note full on the 1-2 most relevant",
            "save_a_chat": "obsidian_save_chat with title, summary, content (dense notation), tags, project, l0, l1. Use custom_date for old chats.",
            "save_multiple_old_chats": "obsidian_batch with list of save_chat ops, each with custom_date set",
            "rename_a_note": "obsidian_move_note(from_path='Folder/OldName.md', to_path='Folder/NewName.md')",
            "add_content_before_related": "obsidian_append_to_note with before_section='## Related'",
            "surgical_edit": "obsidian_patch_section — never use write_note(overwrite=True) just to add a section",
            "find_context_for_a_topic": "obsidian_find_related on a relevant note → obsidian_graph_walk on top results",
            "update_tags_or_metadata": "obsidian_patch_frontmatter — leaves body untouched",
        },
        "gotchas": [
            "save_chat auto-prepends date to filename — do NOT include date in the title param or you get a doubled prefix like '2026-21-03 2026-21-03 Title.md'.",
            "append_to_note without before_section goes to absolute EOF — lands after ## Related and breaks link structure. Use before_section='## Related' when the note has one.",
            "write_note(overwrite=True) rewrites the entire file — use patch_section for partial edits.",
            "move_folder does NOT update [[wikilinks]] pointing to moved notes — links will break.",
            "delete_note is irreversible — relink undo only covers ## Related sections, not deleted files.",
            "backfill_summaries may silently fail if claude_code_sdk can't nest a session inside the MCP process — check errors[] in the response.",
            "graph_walk builds the reverse index (backlinks) on every call with direction='in' or 'both' — slow on large vaults. Use direction='out' when backlinks aren't needed.",
            "relink mode='full' is slow and writes to every note — run sparingly, use undo if results are wrong.",
        ],
    }

    if topic:
        t = topic.lower().strip()
        # Check tool name match
        for cat, data in guide["categories"].items():
            tool_names = [tool.lower() for tool in data["tools"]]
            tool_stems = [tool.replace("obsidian_", "") for tool in tool_names]
            if t in tool_names or t in tool_stems or t == cat.lower():
                return {"topic": topic, "category": cat, **data, "gotchas": [g for g in guide["gotchas"] if t.replace("obsidian_", "") in g.lower()]}
        # Check free-text sections
        for key in ("tier_system", "common_workflows", "gotchas", "overview"):
            if t in key:
                return {"topic": key, "content": guide[key]}
        return {"topic": topic, "note": "No specific entry found. Returning full guide.", **guide}

    return guide


if __name__ == "__main__":
    mcp.run(transport="stdio")
