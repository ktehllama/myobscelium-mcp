# Design: obsidian_read_note — Anchor-Based Range Reading

**Date:** 2026-08-25
**Status:** Approved

## Overview

Extend `obsidian_read_note` with four new optional parameters that let callers read a slice of a file delimited by text anchors rather than line numbers. When an anchor is provided the summary tier is bypassed automatically.

## New Parameters

```python
anchor_start: str | None = None
anchor_start_offset: int = 0
anchor_end: str | None = None
anchor_end_offset: int = 0
```

| Param | Type | Default | Meaning |
|---|---|---|---|
| `anchor_start` | `str \| None` | `None` | Text that the start line must **start with** (case-sensitive). Required to activate anchor mode. |
| `anchor_start_offset` | `int` | `0` | Shift start line by N (positive = down, negative = up). Applied after anchor is found. |
| `anchor_end` | `str \| None` | `None` | Text that the end line must **start with** (case-sensitive). If omitted, reads to EOF. |
| `anchor_end_offset` | `int` | `0` | Shift end line by N (positive = down, negative = up). Applied after anchor is found. |

## Behavior

### Activation
Anchor mode activates when `anchor_start` is set. The `full` parameter and `line_start`/`line_end` are **ignored** in this mode.

### Matching
A line matches an anchor when `line.startswith(anchor_text)`. Case-sensitive. First match wins (top-to-bottom scan).

### Offset application
After locating the matching line index, the offset shifts the effective boundary:
- `anchor_start_offset=2` → skip 2 lines down from the start anchor
- `anchor_end_offset=-2` → end 2 lines before the end anchor

### Inclusivity
Both effective boundaries are **inclusive**. The content returned includes the lines at the final computed start and end positions.

### anchor_end omitted
If `anchor_end` is `None`, the slice runs from the effective start to the last line of the file.

### Error conditions
- `anchor_start` not found → `ToolError("Anchor not found: '<text>'")`
- `anchor_end` not found (when provided) → `ToolError("Anchor not found: '<text>'")`
- Effective start > effective end after offsets are applied → `ToolError("Anchor range is empty after applying offsets")`
- Effective start < 0 or effective end > total lines → clamp silently to file bounds (don't error on over-shoot)

## Example

```
obsidian_read_note(
    path="TASKS.md",
    anchor_start="## 🔴 ACTIVE",
    anchor_end="## ⚪ BACKLOG",
    anchor_end_offset=-2,
)
```

File layout (simplified):
```
line 10: ## 🔴 ACTIVE
...
line 40: ---
line 41: (blank)
line 42: ## ⚪ BACKLOG
```

- `anchor_start` found at line 10, offset 0 → effective start = line 10
- `anchor_end` found at line 42, offset -2 → effective end = line 40
- Returns lines 10–40 inclusive (includes the `---` separator)

## Return Shape

Same as the existing line-range return:
```json
{
  "p": "<path>",
  "content": "<slice text>",
  "total_lines": <int>
}
```

## Implementation Location

`server.py` — `obsidian_read_note` function (~line 620). Add anchor resolution logic between the summary-tier block and the existing line-range block.

## Docstring Update

Add to the function docstring:
> Anchor mode: set `anchor_start` to a string that the target line must start with (case-sensitive). The slice runs from that line to `anchor_end` (or EOF). Both support an integer offset to shift the effective boundary up or down. Raises ToolError if an anchor is not found.

## What Is NOT Changing

- Summary tier behavior (`full=False`, no anchors) — unchanged
- `full=True` with `line_start`/`line_end` — unchanged
- All other tools — unchanged
