# Technical Reference — Myobscelium MCP Server

## Stack

- **Language:** Python 3.11+
- **MCP Framework:** FastMCP (`mcp[cli]`)
- **Transport:** stdio (Claude Desktop spawns the process and communicates over stdin/stdout)
- **Dependencies:**
  - `mcp[cli]` — FastMCP server framework and CLI
  - `pydantic >= 2.0` — data validation
  - `PyYAML >= 6.0` — YAML frontmatter parsing
  - `claude_code_sdk` — async Claude API calls for L0/L1 summary generation

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OBSIDIAN_VAULT_PATH` | `~/Documents/Obsidian Vault` | Absolute path to vault root |
| `OBSIDIAN_CHATS_FOLDER` | `Claude/Chats` | Vault-relative folder for saved conversations |
| `OBSIDIAN_DAILY_FOLDER` | `Daily` | Vault-relative folder for daily notes (informational only) |

---

## Constants

| Constant | Value | Description |
|---|---|---|
| `HAIKU_MODEL` | `"claude-haiku-4-5-20251001"` | Model used for L0/L1 summary generation |
| `SAME_FOLDER_DAMPENING` | `0.4` | Score multiplier for candidates in same folder as target |
| `SAME_FOLDER_MAX_LINKS` | `1` | Max outgoing links allowed to notes in same folder |
| `CROSS_PROJECT_DAMPENING` | `0.3` | Score multiplier when notes have different non-empty `project` fields |
| `RELINK_MIN_SCORE` | `0.3` | Minimum score threshold for relink operations |
| `FIND_RELATED_MIN_SCORE` | `0.05` | Minimum score threshold for find_related discovery |
| `TITLE_WORD_WEIGHT` | `0.1` | Multiplier applied to title word score component |
| `BODY_WORD_WEIGHT` | `0.03` | Multiplier applied per shared body word |
| `BODY_WORDS_MAX_CHARS` | `1500` | Character limit for body word extraction |
| `FRONTMATTER_MAX_BYTES` | `16384` | Max bytes read when parsing frontmatter |
| `SEARCH_PREVIEW_LEN` | `120` | Character length of search result preview snippets |

**Word filter sets:**

- `STOP_WORDS`: `{"the", "a", "an", "and", "or", "of", "in", "to"}` — excluded from all scoring
- `GENERIC_TAGS`: `{"chat", "claude", "conversation", "ai", "note"}` — excluded from IDF tag scoring
- `CONTENT_STOP`: `STOP_WORDS` + ~30 common words including `"about"`, `"would"`, `"section"`, `"content"`, `"obsidian"`, etc. — excluded from body word extraction

---

## Abbreviated Response Keys

All tool responses use single-character keys to minimize token usage.

| Constant | Value | Meaning |
|---|---|---|
| `K_PATH` | `"p"` | Note path relative to vault root |
| `K_CONTENT` | `"c"` | Note body content |
| `K_MODIFIED` | `"m"` | Last modified timestamp (milliseconds since epoch) |
| `K_LINES` | `"lc"` | Line count |
| `K_SCORE` | `"s"` | Relevance score |
| `K_TAGS` | `"st"` | Shared tags list |
| `K_L0` | `"l0"` | One-sentence summary (frontmatter field) |

---

## Path Security

```python
def vault_path(relative: str) -> Path
```

Resolves `relative` against `VAULT_PATH` and calls `.resolve()` on the result. If the resolved path is not relative to `VAULT_PATH`, raises `ToolError`. Prevents `../` traversal attacks on all tool inputs.

---

## Scoring Algorithm

`_find_related_core(path_str, top_k, folder, min_score)` — discovers related notes via three scoring components.

### 1. IDF Tag Score

For each tag shared between target and candidate (excluding `GENERIC_TAGS`):

```
tag_score += 1.0 / frequency(tag)
```

Tags that appear in fewer notes contribute more. Tags in `GENERIC_TAGS` are excluded entirely.

### 2. Title Word Score

Extract words matching `[a-z]{3,}` from both note stems (filenames), minus `CONTENT_STOP`. For each shared word:

```
title_score += (1.0 / frequency(word)) * TITLE_WORD_WEIGHT
```

Word frequency is computed across all scanned notes' stems.

### 3. Body Word Score

Extract words matching `[a-z]{5,}` from the first `BODY_WORDS_MAX_CHARS` characters of each note's body (frontmatter stripped), minus `CONTENT_STOP`. Count shared words:

```
body_score += shared_word_count * BODY_WORD_WEIGHT
```

### Combined Score

```
score = round(tag_score + title_score + body_score, 2)
```

### Dampening

Applied after combining, multiplicatively:

- **Same-folder:** If `candidate.parent == target.parent`:
  ```
  score = round(score * SAME_FOLDER_DAMPENING, 2)
  ```
- **Cross-project:** If both notes have non-empty `project` frontmatter fields and they differ:
  ```
  score = round(score * CROSS_PROJECT_DAMPENING, 2)
  ```

Both can apply to the same candidate (e.g., same folder + different project → `score * 0.4 * 0.3`).

### Pre-index

Before scoring, one full vault scan builds:
- `tag_freq: dict[str, int]` — occurrence count per tag across all notes
- `title_word_freq: dict[str, int]` — occurrence count per word across all note stems

---

## Private Helpers

### File I/O

**`_write_note(p, content, overwrite) -> bool`**
Creates parent dirs. Raises `ToolError` if file exists and `overwrite=False`. Returns `True` if new file created, `False` if overwritten.

**`_move_note(src, dst) -> None`**
Validates src exists and dst does not. Creates dst parent dirs. Uses `Path.rename()` (atomic on same filesystem).

**`_delete_note(p) -> None`**
Raises `ToolError` if file not found. Calls `Path.unlink()`.

**`_append_note(p, content, add_separator) -> None`**
If file exists: reads content, ensures trailing newline, prepends `\n---\n\n` if `add_separator=True` else `\n`, writes. If file absent: writes content directly. Creates parent dirs.

### Frontmatter

**`_parse_frontmatter(p) -> dict | None`**
Reads at most `FRONTMATTER_MAX_BYTES`. Expects `---\n` at byte 0 and finds closing `\n---`. Parses YAML between markers. Returns `None` on any failure (no frontmatter, bad YAML, read error).

**`_inject_summary_into_frontmatter(text, l0, l1) -> str`**
Parses existing frontmatter from `text`. Merges `l0`, `l1`, and `l1_generated` (today's ISO date) into the parsed dict. Rewrites frontmatter block only; body unchanged. No-op if text has no valid frontmatter.

**`_patch_section(p, match, match_type, content, heading_level, create_if_missing) -> str`**
Three modes:
- `"text"`: find-and-replace first occurrence of `match` in file
- `"section"`: calls `_remove_section(p, match, heading_level)`
- `"heading"`: find heading by name (strips leading `#` from match); replace everything between that heading and the next heading at same or higher level; if not found and `create_if_missing=True`, append new section

Returns `"ok"`, `"created"`, or `"not_found"`.

**`_remove_section(p, match, heading_level) -> bool`**
Finds heading by name. Deletes from heading line through end of its content (until next equal-or-higher-level heading). Returns `True` if found and deleted.

### Text Analysis

**`_body_words(p) -> set[str]`**
Reads note, strips frontmatter, takes first `BODY_WORDS_MAX_CHARS` chars. Returns `{w for w in re.findall(r"[a-z]{5,}", ...) if w not in CONTENT_STOP}`. Returns empty set on read error.

**`_infer_project(note_path, fm) -> str`**
Returns `fm["project"]` (lowercased) if present. Otherwise walks `note_path`'s parts; if a part equals `"projects"` and has a following part before the filename, returns that part lowercased. Returns `""` if neither found.

**`_count_md(folder) -> int`**
Counts `.md` files in `folder` (non-recursive, non-dotted).

### Link Resolution

**`_parse_wikilinks(content) -> list[str]`**
Regex: `\[\[([^\]]+)\]\]`. Splits each match on `|` to get stem (drops alias). Strips `.md` suffix. Returns list of stems.

**`_resolve_wikilink(stem) -> Path | None`**
Recursively globs vault for `*.md`. Case-insensitive stem comparison. Skips dotted-path files. Returns first match or `None`.

**`_read_full_content(p) -> str`**
Reads file, strips frontmatter block if present, returns stripped body. Returns `""` on read error.

### Related Section

**`_read_existing_related(text) -> tuple[list[str], set[str]]`**
Finds first heading matching `Related` (case-insensitive, any level). Extracts bullet lines until next same-or-higher heading. Returns `(bullet_lines, {wikilink_title, ...})`.

**`_format_related_entry(title, shared_tags) -> str`**
If `shared_tags` exist: picks non-generic ones (or first 2 if all generic), max 3. Returns `"* [[Title]] — shares #tag1, #tag2"`. If no tags: `"* [[Title]] — related by title"`.

**`_passes_tag_filter(shared_tags, target_stem, note_stem) -> bool`**
Returns `True` if any shared tag is non-generic. Else checks if target and note stems share real words (minus `STOP_WORDS`). Prevents generic-tag-only false matches.

**`_apply_relink(note_path, new_entries, vault_stems) -> str`**
1. Collapse duplicate `## Related` sections (keep first, delete rest)
2. Read existing bullets; validate each wikilink resolves in `vault_stems`
3. Filter `new_entries` for titles already present
4. Call `_patch_section(..., "heading", merged_content)`

Returns `"updated"` or `"no_change"`.

**`_capture_related_state(note_path) -> dict`**
Snapshots pre-relink state. Returns `{"path": str, "had_section": bool, "section_content": str | None}`.

**`_cap_same_folder_links(related, target_parent, max_links) -> list`**
Iterates pre-sorted results. Counts same-folder candidates; drops them once count exceeds `SAME_FOLDER_MAX_LINKS`. Cross-folder results pass through unrestricted.

### MOC Detection

**`_is_moc(path) -> bool`**
Returns `True` if any of:
- Filename stem contains `"moc"`, `"index"`, `"overview"`, or `"hub"`
- Frontmatter `type` field equals `"moc"`
- Note contains 5+ wikilinks that all resolve to notes in the same single folder

**`_build_moc_map() -> dict[str, Path]`**
Globs vault, calls `_is_moc` on each. Returns `{folder_path_str: moc_note_path}` — one MOC per folder.

---

## L0/L1 Summary System

### Generation

```python
async def _generate_summary_async(content: str) -> tuple[str, str]
```

Prompt sent to `claude_code_sdk.query()`:
```
Return JSON only: {"l0": "<one sentence ≤25 words>", "l1": "<2-3 sentences 60-100 words>"}

Note content:
{content[:2000]}
```

`ClaudeCodeOptions(allowed_tools=[])` — no tools, pure text response. Extracts first valid JSON object from response text via `re.finditer(r'\{[^{}]*\}', ...)`. Silent fail returns `("", "")`.

Sync wrapper: `_generate_summary(content)` calls `asyncio.run(_generate_summary_async(content))`.

### Injection

`_inject_summary_into_frontmatter(text, l0, l1)` merges into existing frontmatter:
- `l0`: one-sentence summary
- `l1`: paragraph overview
- `l1_generated`: ISO date of generation (used for staleness check in `obsidian_backfill_summaries`)

Auto-generation triggers in `obsidian_write_note` when: content has valid frontmatter AND body > 200 chars AND `l0` not provided.

---

## Relink System

### Undo Stack

Stored as `.relink-undo.json` at vault root. Format:

```json
[
  {
    "timestamp": "2026-03-29T10:00:00",
    "mode": "normal",
    "entries": [
      {"path": "rel/path.md", "had_section": true, "section_content": "* [[Note]] — shares #tag"}
    ]
  },
  ...
]
```

Stack capped at 5 entries. Migrates old single-dict format to list on read.

### Mode: normal

1. Find most recent `.md` in `CHATS_FOLDER` by mtime
2. `_find_related_core(target, top_k=10, min_score=min_score)`
3. Filter: `_passes_tag_filter` + `_is_excluded`
4. `_cap_same_folder_links`
5. Snapshot + `_apply_relink`

### Mode: extended

Same as normal but:
- Searches entire vault (not just Chats)
- Builds MOC map; suppresses candidates where both target and candidate appear in their shared folder's MOC wikilinks
- Skips if target itself is a MOC note

### Mode: full

1. Collect all non-excluded vault notes
2. For each: run `_find_related_core`, filter, cap
3. **Dedup:** Track `seen_pairs: set[frozenset]`; for each `(A, B)` pair, keep only first-encountered direction — prevents symmetric A→B and B→A links in same pass
4. Snapshot all notes touching, apply relinks

### Mode: orphan

1. Collect chat notes with zero outgoing wikilinks AND zero backlinks (builds reverse index for backlink check)
2. For each orphan: run extended-style vault-wide scoring with MOC suppression
3. Apply relinks individually

### Mode: undo

1. Load stack; pop index 0
2. For each entry: if `had_section=True`, call `_patch_section(..., "heading", section_content)`; else `_remove_section`
3. Write remaining stack back (or delete file if empty)

### smart=True

Available in `normal` and `extended` only. Instead of writing, returns:
```json
{
  "status": "review_pending",
  "target": "path",
  "target_content": "...",
  "candidates": [{"p": "...", "content": "...", "score": 0.5, ...}],
  "instruction": "..."
}
```

Uses `top_k=20` and `FIND_RELATED_MIN_SCORE` instead of `RELINK_MIN_SCORE`.

### MOC-Aware Suppression

For extended/orphan/full: load `_build_moc_map()`. For each candidate in same folder as target:

```
if folder has MOC:
    if target.stem in moc_wikilinks[folder] AND candidate.stem in moc_wikilinks[folder]:
        suppress candidate
```

Prevents redundant same-folder links already captured by the MOC.

---

## BFS Graph Walk

`obsidian_graph_walk(path, depth, direction, include_l0)`

1. If `direction` in `("in", "both")`: build reverse index — scan all notes, parse wikilinks, map `target_stem → {files_that_link_to_it}`
2. BFS from source at depth 0:
   - For each node at `current_depth < depth`:
     - `"out"`: parse wikilinks from note, resolve each, add as out-neighbors
     - `"in"`: look up reverse index for current node, add as in-neighbors
     - `"both"`: union; if neighbor in both, set `dir="both"`
   - Skip already-visited nodes (each node processed once)
3. Result: `{neighbor_path: {"degree": int, "dir": str, "l0": str}}`

Depth clamped to `[1, 6]`. Reverse index built once per call.

---

## MCP Tools

### obsidian_vault_overview
| Param | Type | Default | Notes |
|---|---|---|---|
| `mode` | `"compact"\|"tree"` | `"compact"` | compact=folder→count dict; tree=indented string |
| `max_depth` | `int` | `3` | max nesting depth |

Returns: `{"folders": {path: count}}` or `{"tree": str}`

---

### obsidian_read_note
| Param | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | — | vault-relative path |
| `line_start` | `int\|None` | `None` | 1-indexed |
| `line_end` | `int\|None` | `None` | inclusive |
| `summary_only` | `bool` | `False` | returns l0+l1+line_count only |

Returns: `{"p", "content", "total_lines"}` or `{"p", "l0", "l1", "lc"}` (summary_only)

---

### obsidian_read_frontmatter
| Param | Type | Default |
|---|---|---|
| `path` | `str` | — |

Returns: `{"p", "frontmatter": dict|None}`

---

### obsidian_write_note
| Param | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | — | creates parent dirs |
| `content` | `str` | — | full note text |
| `overwrite` | `bool` | `False` | |
| `l0` | `str` | `""` | auto-generated if body >200 chars and l0 empty |
| `l1` | `str` | `""` | |

Returns: `{"p", "created": bool, "hint": str|None}`
Hint present for `.base` or `.canvas` files.

---

### obsidian_append_to_note
| Param | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | — | |
| `content` | `str` | — | |
| `add_separator` | `bool` | `False` | inserts `\n---\n\n` before content |
| `before_section` | `str\|None` | `None` | insert before this heading instead of end |

Returns: `{"p", "appended_bytes": int, ["inserted_before": str]}`

---

### obsidian_move_note
| Param | Type |
|---|---|
| `from_path` | `str` |
| `to_path` | `str` |

Returns: `{"from", "to"}`

---

### obsidian_delete_note
| Param | Type | Default |
|---|---|---|
| `path` | `str` | — |
| `confirm` | `bool` | `False` |

Raises `ToolError` if `confirm=False`. Returns: `{"p", "deleted": True}`

---

### obsidian_list_folder
| Param | Type | Default | Notes |
|---|---|---|---|
| `folder` | `str` | `""` | empty = vault root |
| `include_preview` | `bool` | `False` | adds `"v"` field (120 chars, no frontmatter) |
| `recursive` | `bool` | `False` | rglob instead of glob |
| `names_only` | `bool` | `False` | flat list of relative paths, ignores other params |

Returns (names_only): `{"folder", "paths": [str]}`
Returns (recursive): `{"folder", "items": [{"p", "m", ["v"]}]}`
Returns (flat): `{"folder", "items": [{"name", "type": "note"|"folder", "m", ["notes", "v"]}]}`

---

### obsidian_save_chat
| Param | Type | Default | Notes |
|---|---|---|---|
| `title` | `str` | — | date auto-prepended to filename |
| `summary` | `str` | — | written to frontmatter |
| `content` | `str` | — | |
| `tags` | `list[str]\|None` | `None` | `["claude","chat"]` always added |
| `project` | `str` | `""` | written to frontmatter |
| `folder` | `str` | `""` | default: `CHATS_FOLDER` |
| `l0` | `str` | `""` | auto-generated if empty |
| `l1` | `str` | `""` | |
| `custom_date` | `str\|None` | `None` | YYYY-MM-DD; overrides today |

Returns: `{"p", "appended": bool, ["warn": str]}`
If note with same date+title exists: appends `## HH:MM — title` section. Else creates new note with full frontmatter.

---

### obsidian_search
| Param | Type | Default | Notes |
|---|---|---|---|
| `query` | `str` | — | |
| `folder` | `str` | `""` | |
| `case_sensitive` | `bool` | `False` | |
| `max_results` | `int` | `20` | |
| `max_scan` | `int` | `500` | |
| `literal` | `bool` | `True` | if False, query treated as regex |
| `group_by_file` | `bool` | `True` | |
| `include_content` | `bool` | `False` | |
| `content_max_chars` | `int` | `500` | |
| `modified_after` | `str\|None` | `None` | ISO 8601 |
| `modified_before` | `str\|None` | `None` | ISO 8601 |
| `tags` | `list[str]\|None` | `None` | filter by frontmatter tags |
| `tier` | `"l0"\|"l1"\|"full"` | `"full"` | l0/l1: return only that frontmatter field per match |

Returns: `{"query", "files"|"matches": [...], "truncated": bool, "scanned": int}`

---

### obsidian_batch
| Param | Type | Default |
|---|---|---|
| `operations` | `list[dict]` | — |
| `confirm` | `bool` | `False` |

Executes 2+ operations in one call. Each op is independent — failures don't abort others. Per-op results include `"index"` and `"ok"`.

**Operation schemas:**

```json
{"op": "write",    "path": "...", "content": "...", "overwrite": false}
{"op": "append",   "path": "...", "content": "...", "add_separator": false}
{"op": "move",     "path": "...", "to": "..."}
{"op": "delete",   "path": "...", "confirm": true}
{"op": "patch_section", "path": "...", "match": "...", "match_type": "heading|text|section",
                         "content": "...", "heading_level": null, "create_if_missing": true}
{"op": "find_related",  "path": "...", "max_results": 10, "folder": "", "min_score": 0.05}
{"op": "save_chat",     "title": "...", "summary": "...", "content": "...",
                         "tags": [], "project": "", "folder": "", "l0": "", "l1": "", "custom_date": null}
```

Returns: `{"results": [...], "success_count": int, "error_count": int}`

---

### obsidian_patch_frontmatter
| Param | Type | Default |
|---|---|---|
| `path` | `str` | — |
| `updates` | `dict` | — |
| `remove_keys` | `list[str]\|None` | `None` |

Merges `updates` into frontmatter dict. Pops `remove_keys`. Rewrites only frontmatter block. Returns: `{"p", "frontmatter": dict}`

---

### obsidian_move_folder
| Param | Type | Default | Notes |
|---|---|---|---|
| `from_folder` | `str` | — | |
| `to_folder` | `str` | — | |
| `overwrite` | `bool` | `False` | if True, merges into existing dest |

Does **not** update `[[wikilinks]]` pointing to moved notes.
Returns: `{"from", "to", "moved": int, "skipped": int}`

---

### obsidian_find_related
| Param | Type | Default |
|---|---|---|
| `path` | `str` | — |
| `top_k` | `int` | `10` |
| `folder` | `str` | `""` |
| `min_score` | `float` | `0.05` |

Calls `_find_related_core`, trims result to `{K_PATH, K_SCORE, K_TAGS, K_L0}`.
Returns: `{"source", "related": [{"p", "s", "st", "l0"}]}`

---

### obsidian_patch_section
| Param | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | — | |
| `match` | `str` | — | heading name, text string, or section name |
| `match_type` | `"heading"\|"text"\|"section"` | — | |
| `content` | `str` | `""` | replacement content |
| `heading_level` | `int\|None` | `None` | constrain to specific `##` level |
| `create_if_missing` | `bool` | `True` | |

Returns: `{"p", "status": "ok"|"created"|"not_found"}`

---

### obsidian_backfill_summaries
| Param | Type | Default | Notes |
|---|---|---|---|
| `folder` | `str` | `""` | scope |
| `limit` | `int` | `50` | max notes to process |
| `overwrite_stale_days` | `int` | `90` | regenerate if `l1_generated` older than N days |

Skips notes that have `l0` and non-stale `l1_generated`. Calls `_generate_summary` then `obsidian_patch_frontmatter`.
Returns: `{"processed": int, "skipped": int, "errors": [str]}`

---

### obsidian_graph_walk
| Param | Type | Default | Notes |
|---|---|---|---|
| `path` | `str` | — | |
| `depth` | `int` | `2` | clamped to [1, 6] |
| `direction` | `"out"\|"in"\|"both"` | `"both"` | |
| `include_l0` | `bool` | `True` | |

Returns: `{"source", "nodes": {path: {"degree": int, "dir": str, "l0": str}}}`

---

### obsidian_relink
| Param | Type | Default |
|---|---|---|
| `mode` | `"normal"\|"extended"\|"full"\|"orphan"\|"undo"` | `"normal"` |
| `min_score` | `float` | `0.3` |
| `exclude_folders` | `list[str]\|None` | `None` |
| `smart` | `bool` | `False` |

See [Relink System](#relink-system) section above for full mode descriptions.

---

### obsidian_help
| Param | Type | Default |
|---|---|---|
| `topic` | `str` | `""` |

Returns full usage guide dict, or filtered subset if topic matches a tool name or category.

---

## Skills (Read-Only Resources)

Registered via `register_skills(mcp)` in `skills.py`. Fetched with `ReadResource`.

| URI | Content |
|---|---|
| `skill://obsidian-markdown` | Obsidian Flavored Markdown spec: wikilinks, embeds, callouts, properties, tags, comments, LaTeX, Mermaid, footnotes |
| `skill://obsidian-bases` | `.base` file format: views, filters, formulas, summaries |
| `skill://json-canvas` | JSON Canvas 1.0 spec: nodes (text/file/link/group), edges, connections, layout |
