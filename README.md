<div align="center">

<img src="docs/obsi_logo.png" width="200" alt="Myobscelium">

# Myobscelium

**A Python MCP server that gives Claude long-term memory and full context of everything, anywhere.**

*In daily production, tracking its own development since day one*

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-FastMCP-FF6B35?style=for-the-badge)
![Obsidian](https://img.shields.io/badge/Obsidian-vault-7C3AED?style=for-the-badge&logo=obsidian&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-Desktop-D4A027?style=for-the-badge)

</div>

---

## The Obsidian-Claude relationship evolution

I'd seen the Obsidian second-brain videos where people connect their vaults to AI tools, treating their notes as a knowledge layer. Most of what I found either ran Claude directly inside Obsidian through a plugin, or piped things through some clunky workaround. From what I'd seen, nobody had built a proper MCP server for it. That seemed wrong, the most logical and frictionless way would be to use the second brain directly from Claude.

The actual push came from how I was already working. Claude is where I think through the most: architecture decisions, debugging sessions, code reviews, research. After a long session I'd generate a summary, copy it into Obsidian by hand, and move on. Three minutes, sounds trivial, until you notice you're doing it every single day and still managing to lose context half the time.

So instead of continuing the copy-paste routine, I built the bridge.

---

## What it does

Myobscelium is an MCP server with 19 tools that sits between Claude Desktop and an Obsidian vault. It handles the full lifecycle: saving chats with structured frontmatter and auto-generated summaries, searching by full text or tags or date, moving and patching notes surgically, and maintaining a wikilink graph across the whole vault.

The part I use most is context retrieval. You point it at a project note and `obsidian_graph_walk` traverses the existing wikilink graph outward from there, returning every related note up to N degrees of separation. The IDF scoring engine finds notes you haven't linked yet by comparing tags, title words, and body content across the vault, weighted by rarity. Common tags like `claude` or `chat` get filtered out before scoring so they don't make everything look related. The relink system then writes those connections automatically, building the `## Related` sections that make graph traversal useful in the first place.

Claude Code also has its own `/obsi` skill where it can use Myobscelium. That means all Claude sessions, whether from Desktop or Code, are stored in the same vault for maximum context. Ask Claude to find context on a project and it can pull from a year of conversations, decisions, and architecture notes, regardless of which interface produced them.

The name comes from mycelium, the fungal network that runs under a forest floor passing signals between trees. That's what this does, invisibly, across the vault.

---

## The context problem

The standard assumption is that if you have a powerful enough model, context is a detail you can work around. It's not. Claude gives a confident answer about a project, but it's working from a month-old understanding because nothing in the session window told it what changed. The knowledge exists, somewhere across a hundred previous conversations. It just can't reach it.

Obsidian already solves the storage half. Notes are Markdown, they're local, they're structured. The missing piece was giving Claude a way to load the right context without reading everything (expensive) or guessing what to look for (unreliable).

Myobscelium handles that with a tiered retrieval system. Every saved note gets two summary fields written to its frontmatter: `l0` is a single sentence, `l1` is two or three sentences. When Claude needs to orient itself in a project, it loads the `l0` layer first and only reads full note content for the notes that actually warrant it. The approach came from watching how OpenViking handled context tiers, adapted to fit how an Obsidian vault actually works.

---

## The relink system

This was the ugliest problem to get right.

The first crude version worked as three separate tool calls, like a pseudo function: run `find_related` on a note, check the results, write the links manually. Functional, but slow. So I built `obsidian_relink` to handle all of it in one call, and that's when the real problems started.

The IDF scorer matched "Django Overview" to "Meridian Sage Overview" because both had the word *overview* in the title. Notes tagged `claude` and `chat` linked to every other note in the vault because those tags were everywhere. After a few full-vault relink runs the graph looked like a blob: hundreds of notes all weakly pointing at each other, no structure to it at all.

The fix was a design philosophy, not a threshold tweak. Links have to be earned, they have to actually matter. Generic tags don't count toward the match score. Same-folder candidates get a 0.4x score penalty and can only receive one incoming link, so a project folder doesn't become self-referential. If a folder already has a Map of Content note, relink won't add direct cross-links between the notes it curates, the MOC is the connection, more links are just noise. Directionality is enforced too: a new note links out to established ones, not the other way around.

There are five modes. Normal links the most recent note to the vault. Extended does the same with MOC-aware suppression on. Full is for a fresh vault where everything is unlinked. Orphan finds notes that were saved after the last relink and missed the window. Undo rolls back the last run, with a 5-tier cache so if you accidentally run relink twice, you can still restore the vault to exactly how it was before. Manually undoing wikilinks across a big vault would be practically impossible. The cache makes it trivial.

---

## Why it works

This README is clear proof.

To write it, I needed context on everything I'd built, all documented in Obsidian. So I asked Claude to get it all. Claude loaded context on six separate build sessions spanning three weeks: code refactor notes, a v2 PRD, the smart relink session, the graph cleanup audit, all of it, without me providing a single filename. The vault has every chat saved since I opened this account. Graph walk and IDF scoring handled the retrieval. I said "find context on the Myobscelium project" and it was already there.

There's a parallel to how Linus Torvalds built Git and Git ended up tracking its own development. Same thing happened here: the system designed to give Claude context on my projects started tracking its own construction in real time, session by session.

---

## Engineering challenges

**Token efficiency.** All 19 tool responses use single-character keys (`p` for path, `c` for content, `m` for modified timestamp) to cut the per-call token cost. Tool descriptions in the MCP schema are trimmed to one line each, saving roughly 400-600 tokens at session start. The tiered summary system means Claude rarely needs a full note read to decide whether it's relevant, most orientation happens at the `l0` layer.

**IDF scoring without false positives.** Inverse document frequency means rare signals score higher than common ones. The problem is that in a vault full of Claude chat saves, tags like `claude`, `mcp`, and `obsidian` appear in almost every file. They're technically meaningful but useless for finding specific relationships. A `GENERIC_TAGS` filter removes them from scoring entirely, and a body word stop list excludes common vault vocabulary before the frequency calculation runs.

**Dampening without over-suppression.** The same-folder penalty (0.4x) stops adjacent notes from linking just because they're nearby. The cross-project penalty (0.3x) stops a Meridian Sage session note from linking to a factur2d2 note just because both mention Raspberry Pi. Stack both on the same candidate and the score gets multiplied by 0.12, which kills real matches along with noise. Threshold calibration here took several iterations to get right.

**MOC detection.** A Map of Content note is explicitly marked in frontmatter, has a name containing "MOC", "Index", "Overview", or "Hub", or contains five or more wikilinks to notes in its folder. Getting that heuristic wrong in either direction breaks the graph: miss a MOC and the folder blobs, over-detect and legitimate notes get skipped. A bug fixed during the GitHub sanitization session was exactly this, "GitHub" in a note title was tripping the MOC detector and causing relink to skip the note entirely.

---

## Stack

| Layer | Tech |
|---|---|
| Language | Python 3.11+ |
| MCP framework | FastMCP (`mcp[cli]`) |
| Transport | stdio |
| Summaries | Claude Code SDK (Haiku, via `claude login`) |
| Vault format | Obsidian Markdown with YAML frontmatter |

---

## Running locally

```bash
git clone https://github.com/ktehllama/myobscelium-mcp
cd myobscelium-mcp
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Set `OBSIDIAN_VAULT_PATH` to your vault root, then add to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "myobscelium": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/myobscelium-mcp/server.py"],
      "env": {
        "OBSIDIAN_VAULT_PATH": "/path/to/your/vault"
      }
    }
  }
}
```

Full configuration reference and all 19 tools documented in [`docs/TECHNICAL.md`](docs/TECHNICAL.md).
