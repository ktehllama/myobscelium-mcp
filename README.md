# Obsidian MCP Server

A Model Context Protocol (MCP) server that bridges Claude Desktop and your Obsidian vault. Claude can read, write, search, and organize notes directly inside your vault, save conversations as structured Obsidian notes, and use bundled skill references for Obsidian Markdown, Bases, and JSON Canvas file formats.

## Prerequisites

- Python 3.11 or later
- pip
- An existing Obsidian vault on your local filesystem
- Claude Desktop (macOS or Windows)

## Installation

```bash
git clone https://github.com/your-username/obsidian-claude-mcp.git
cd obsidian-claude-mcp
pip install -r requirements.txt
```

## Configuration

Add the server to Claude Desktop's configuration file.

**macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows** — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["/absolute/path/to/obsidian-claude-mcp/server.py"],
      "env": {
        "OBSIDIAN_VAULT_PATH": "/absolute/path/to/your/vault"
      }
    }
  }
}
```

On Windows, use forward slashes or escaped backslashes in paths:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["C:/Users/you/projects/obsidian-claude-mcp/server.py"],
      "env": {
        "OBSIDIAN_VAULT_PATH": "C:/Users/you/MyVault"
      }
    }
  }
}
```

Restart Claude Desktop after saving the configuration file.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OBSIDIAN_VAULT_PATH` | Yes | — | Absolute path to your Obsidian vault root |
| `OBSIDIAN_CHATS_FOLDER` | No | `Chats` | Vault folder where saved conversations are stored |
| `OBSIDIAN_DAILY_FOLDER` | No | `Daily` | Vault folder for daily notes (informational; not auto-created) |

## Recommended Vault Structure

Create a `Claude/` folder inside your vault to keep AI-related content organized:

```
YourVault/
├── Claude/
│   ├── Chats/          # Saved Claude conversations (set OBSIDIAN_CHATS_FOLDER=Claude/Chats)
│   └── Scratch/        # Temporary working notes
├── Daily/              # Daily notes
├── Projects/           # Project folders
├── Areas/              # Ongoing areas of responsibility
├── Resources/          # Reference material
└── Archive/            # Archived notes
```

## Tools Reference

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `obsidian_vault_overview` | Return folder structure with note counts | `mode` (`compact`/`tree`), `max_depth` |
| `obsidian_read_note` | Read a note, optionally by line range | `path`, `line_start`, `line_end` |
| `obsidian_read_frontmatter` | Read only the YAML frontmatter of a note | `path` |
| `obsidian_write_note` | Create or overwrite a note | `path`, `content`, `overwrite` |
| `obsidian_append_to_note` | Append content to a note, creating it if absent | `path`, `content`, `add_separator` |
| `obsidian_move_note` | Move or rename a note | `from_path`, `to_path` |
| `obsidian_delete_note` | Delete a note (requires explicit confirmation) | `path`, `confirm` |
| `obsidian_list_folder` | List notes and subfolders in a vault folder | `folder`, `include_preview` |
| `obsidian_save_chat` | Save a Claude conversation as a formatted note | `title`, `summary`, `content`, `tags`, `project`, `folder` |
| `obsidian_search` | Full-text search across vault notes | `query`, `folder`, `case_sensitive`, `max_results` |
| `obsidian_batch` | Execute multiple write/move/delete operations in one call | `operations` (list of op dicts) |

### obsidian_batch Operation Format

Each operation in the `operations` list is a dict with an `op` field set to `"write"`, `"move"`, or `"delete"`:

```json
[
  {"op": "write", "path": "Notes/foo.md", "content": "# Foo", "overwrite": false},
  {"op": "move",  "path": "Notes/old.md", "to": "Archive/old.md"},
  {"op": "delete","path": "Notes/tmp.md", "confirm": true}
]
```

Each operation is attempted independently; failures are reported per-operation without aborting the batch.

## Resources (Skills)

The server exposes three read-only skill references that Claude can fetch to understand Obsidian file formats before writing them:

| URI | Description | When to Use |
|-----|-------------|-------------|
| `skill://obsidian-markdown` | Obsidian Flavored Markdown syntax | Creating or editing `.md` notes with wikilinks, callouts, embeds, or properties |
| `skill://obsidian-bases` | Obsidian Bases `.base` file format | Creating or editing `.base` database view files |
| `skill://json-canvas` | JSON Canvas `.canvas` file format | Creating or editing `.canvas` visual canvas files |

Claude is automatically prompted to read these resources when writing `.base` or `.canvas` files via `obsidian_write_note`.

## Security

All file paths are resolved against the vault root before any filesystem operation. Any path that resolves outside the vault directory (e.g., via `../` traversal) is rejected with an error before touching the filesystem. Claude cannot access files outside your configured vault, regardless of what path is passed to a tool.

## Limitations

- **Search performance**: `obsidian_search` performs a linear scan of all `.md` files. On vaults with tens of thousands of notes, searches may be slow. Scope searches to a subfolder using the `folder` parameter when possible.
- **Text files only**: The server reads and writes `.md`, `.base`, and `.canvas` files as UTF-8 text. Binary files (images, PDFs, audio) cannot be read or written through this server.
- **No real-time sync**: Changes made by Obsidian while a tool call is in progress are not detected; last write wins.
- **No plugin API access**: This server works at the filesystem level. Obsidian plugin features (Dataview queries, Templater execution, graph indexing) are not accessible.

## Example Prompts

Here are things you can say to Claude Desktop once the server is configured:

- "Show me the structure of my vault and list the folders."
- "Search my vault for notes mentioning 'project alpha' and summarize what you find."
- "Create a new note in Projects/Alpha/ called Meeting Notes with today's date and key decisions from our discussion."
- "Save this conversation to my Chats folder under the title 'Python Refactoring Session' with the tag 'engineering'."
- "Move all notes in my Scratch folder that are older than last month to Archive/Scratch."
- "Read my Projects/Alpha/README.md and suggest improvements to the structure, then write the updated version back."
