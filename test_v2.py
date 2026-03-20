import sys, os, tempfile, shutil
from pathlib import Path
from contextlib import contextmanager

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))
import server

try:
    from fastmcp.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp.exceptions import ToolError

_ORIG_VAULT = server.VAULT_PATH
_ORIG_CHATS = server.CHATS_FOLDER


@contextmanager
def temp_vault(chats_folder="Chats"):
    tmpdir = Path(tempfile.mkdtemp())
    server.VAULT_PATH = tmpdir
    server.CHATS_FOLDER = chats_folder
    try:
        yield tmpdir
    finally:
        server.VAULT_PATH = _ORIG_VAULT
        server.CHATS_FOLDER = _ORIG_CHATS
        shutil.rmtree(tmpdir, ignore_errors=True)


def write_note(vault, rel, content):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def fm(tags, extra=None):
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    base = f"---\ntags:\n{tag_lines}\n"
    if extra:
        for k, v in extra.items():
            base += f"{k}: {v}\n"
    return base + "---\n\n"


RESULTS = []


def record(name, passed, detail=""):
    RESULTS.append((name, passed, detail))
    marker = "[PASS]" if passed else "[FAIL]"
    suffix = ""
    if detail:
        dlines = detail.splitlines()
        suffix = "\n        " + "\n        ".join(dlines)
    print(f"  {marker} {name}{suffix}")


def run_test(name, fn):
    try:
        fn()
        record(name, True)
    except AssertionError as e:
        record(name, False, str(e))
    except Exception as e:
        record(name, False, f"{type(e).__name__}: {e}")


# ─── T-TOKEN ─────────────────────────────────────────────────────────────────

def test_token_summary_only():
    with temp_vault() as v:
        content = "---\ntags:\n  - python\nl0: brief summary\nl1: longer summary text here\n---\n\nBody content.\n"
        write_note(v, "note.md", content)
        result = server.obsidian_read_note("note.md", summary_only=True)
        assert "p" in result, f"p key missing: {result}"
        assert "l0" in result, f"l0 key missing: {result}"
        assert "l1" in result, f"l1 key missing: {result}"
        assert "lc" in result, f"lc key missing: {result}"
        assert result["l0"] == "brief summary", f"l0 wrong: {result}"
        assert result["l1"] == "longer summary text here", f"l1 wrong: {result}"
        assert "content" not in result, f"content should be absent: {result}"


def test_token_summary_only_missing_l0():
    with temp_vault() as v:
        write_note(v, "note.md", "---\ntags:\n  - python\n---\n\nBody.\n")
        result = server.obsidian_read_note("note.md", summary_only=True)
        assert result["l0"] == "", f"l0 should be empty string: {result}"
        assert result["l1"] == "", f"l1 should be empty string: {result}"


def test_token_names_only():
    with temp_vault() as v:
        write_note(v, "a.md", "content a")
        write_note(v, "b.md", "content b")
        write_note(v, "sub/c.md", "content c")
        result = server.obsidian_list_folder("", names_only=True, recursive=True)
        assert "paths" in result, f"paths key missing: {result}"
        assert isinstance(result["paths"], list), f"paths not list: {result}"
        assert any("a.md" in p for p in result["paths"]), f"a.md missing: {result}"
        assert any("b.md" in p for p in result["paths"]), f"b.md missing: {result}"
        assert any("c.md" in p for p in result["paths"]), f"c.md missing: {result}"
        assert "items" not in result, f"items should be absent: {result}"


def test_token_names_only_non_recursive():
    with temp_vault() as v:
        write_note(v, "a.md", "content a")
        write_note(v, "sub/b.md", "content b")
        result = server.obsidian_list_folder("", names_only=True)
        assert any("a.md" in p for p in result["paths"]), f"a.md missing: {result}"
        assert not any("b.md" in p for p in result["paths"]), f"b.md should not appear (not recursive): {result}"


def test_token_search_tier_l0():
    with temp_vault() as v:
        write_note(v, "note.md", "---\ntags:\n  - test\nl0: my l0 summary\n---\n\npython async keyword\n")
        result = server.obsidian_search("python", tier="l0")
        assert len(result["files"]) == 1, f"expected 1 result: {result}"
        entry = result["files"][0]
        assert "l0" in entry, f"l0 key missing in entry: {entry}"
        assert entry["l0"] == "my l0 summary", f"l0 wrong: {entry}"
        assert "content" not in entry, f"content should be absent: {entry}"


def test_token_search_tier_full_default():
    with temp_vault() as v:
        write_note(v, "note.md", "python async keyword content here")
        result = server.obsidian_search("python", tier="full", include_content=True)
        assert len(result["files"]) == 1, f"expected 1 result: {result}"
        entry = result["files"][0]
        assert "content" in entry, f"content key missing: {entry}"


def test_token_find_related_abbreviated_keys():
    with temp_vault() as v:
        write_note(v, "A.md", fm(["python", "async"]) + "# A\ncontent")
        write_note(v, "B.md", fm(["python", "async"]) + "# B\ncontent")
        result = server.obsidian_find_related("A.md")
        assert "related" in result, f"related missing: {result}"
        if result["related"]:
            entry = result["related"][0]
            assert "p" in entry, f"p key missing: {entry}"
            assert "s" in entry, f"s key missing: {entry}"
            assert "st" in entry, f"st key missing: {entry}"
            assert "l0" in entry, f"l0 key missing: {entry}"
            assert "title" not in entry, f"title should not be in trimmed response: {entry}"
            assert "score" not in entry, f"score should not be in trimmed response (use 's'): {entry}"


def test_token_search_content_max_default():
    """Default content_max_chars should be 500 (not 2000)."""
    import inspect
    sig = inspect.signature(server.obsidian_search)
    default = sig.parameters["content_max_chars"].default
    assert default == 500, f"expected 500, got {default}"


# ─── T-APPEND ────────────────────────────────────────────────────────────────

def test_append_before_section_found():
    with temp_vault() as v:
        content = "# Title\n\nBody text.\n\n## Notes\n\nsome notes\n\n## References\n\nrefs\n"
        p = write_note(v, "note.md", content)
        result = server.obsidian_append_to_note("note.md", "INSERTED", before_section="Notes")
        text = p.read_text(encoding="utf-8")
        assert "INSERTED" in text, f"inserted content missing: {text!r}"
        assert text.index("INSERTED") < text.index("## Notes"), "INSERTED should be before ## Notes"
        assert "## References" in text, "References section should be preserved"
        assert "inserted_before" in result, f"inserted_before key missing: {result}"


def test_append_before_section_not_found_fallback():
    with temp_vault() as v:
        content = "# Title\n\nBody text.\n"
        p = write_note(v, "note.md", content)
        result = server.obsidian_append_to_note("note.md", "APPENDED", before_section="NonExistent")
        text = p.read_text(encoding="utf-8")
        assert "APPENDED" in text, f"content missing: {text!r}"
        assert text.endswith("APPENDED") or "APPENDED" in text, f"content not appended: {text!r}"


def test_append_before_section_none_normal():
    with temp_vault() as v:
        p = write_note(v, "note.md", "existing content\n")
        server.obsidian_append_to_note("note.md", "new content", before_section=None)
        text = p.read_text(encoding="utf-8")
        assert "existing content" in text
        assert "new content" in text
        assert text.index("existing content") < text.index("new content")


def test_append_before_section_case_insensitive():
    with temp_vault() as v:
        content = "# Title\n\n## Related\n\nlinks\n"
        p = write_note(v, "note.md", content)
        server.obsidian_append_to_note("note.md", "BEFORE RELATED", before_section="related")
        text = p.read_text(encoding="utf-8")
        assert text.index("BEFORE RELATED") < text.index("## Related"), "insert should be before ## Related"


# ─── T-BACKFILL ──────────────────────────────────────────────────────────────

def test_backfill_generates_summaries(monkeypatch=None):
    with temp_vault() as v:
        # Patch _generate_summary to avoid real API calls
        original = server._generate_summary
        server._generate_summary = lambda c: ("short l0", "longer l1 text")
        try:
            write_note(v, "no_summary.md", "---\ntags:\n  - test\n---\n\nThis is a long body with lots of content to summarize properly.\n")
            result = server.obsidian_backfill_summaries(limit=10)
            assert "processed" in result, f"processed key missing: {result}"
            assert "skipped" in result, f"skipped key missing: {result}"
            assert "errors" in result, f"errors key missing: {result}"
            assert result["processed"] >= 1, f"expected at least 1 processed: {result}"
            # Check frontmatter was updated
            fm = server._parse_frontmatter(v / "no_summary.md") or {}
            assert fm.get("l0") == "short l0", f"l0 not written: {fm}"
            assert fm.get("l1") == "longer l1 text", f"l1 not written: {fm}"
            assert "l1_generated" in fm, f"l1_generated not written: {fm}"
        finally:
            server._generate_summary = original


def test_backfill_skips_fresh_summaries():
    with temp_vault() as v:
        from datetime import date
        today = date.today().isoformat()
        write_note(v, "has_summary.md", f"---\ntags:\n  - test\nl0: existing\nl1: existing long\nl1_generated: {today}\n---\n\nBody.\n")
        original = server._generate_summary
        calls = []
        server._generate_summary = lambda c: (calls.append(c), ("new l0", "new l1"))[1]
        try:
            result = server.obsidian_backfill_summaries(limit=10, overwrite_stale_days=90)
            assert result["skipped"] >= 1, f"should skip fresh note: {result}"
            assert len(calls) == 0, f"should not call _generate_summary for fresh note"
        finally:
            server._generate_summary = original


def test_backfill_overwrites_stale():
    with temp_vault() as v:
        write_note(v, "stale.md", "---\ntags:\n  - test\nl0: old\nl1_generated: 2020-01-01\n---\n\nBody.\n")
        original = server._generate_summary
        server._generate_summary = lambda c: ("fresh l0", "fresh l1")
        try:
            result = server.obsidian_backfill_summaries(limit=10, overwrite_stale_days=90)
            assert result["processed"] >= 1, f"stale note should be processed: {result}"
            fm = server._parse_frontmatter(v / "stale.md") or {}
            assert fm.get("l0") == "fresh l0", f"l0 not updated: {fm}"
        finally:
            server._generate_summary = original


def test_backfill_respects_limit():
    with temp_vault() as v:
        for i in range(5):
            write_note(v, f"note{i}.md", f"---\ntags:\n  - test\n---\n\nBody content {i}.\n")
        original = server._generate_summary
        server._generate_summary = lambda c: ("l0", "l1")
        try:
            result = server.obsidian_backfill_summaries(limit=2)
            assert result["processed"] <= 2, f"limit not respected: {result}"
        finally:
            server._generate_summary = original


def test_backfill_folder_scoping():
    with temp_vault() as v:
        write_note(v, "A/note.md", "---\ntags:\n  - test\n---\n\nBody.\n")
        write_note(v, "B/note.md", "---\ntags:\n  - test\n---\n\nBody.\n")
        processed_paths = []
        original = server._generate_summary
        server._generate_summary = lambda c: ("l0", "l1")
        try:
            result = server.obsidian_backfill_summaries(folder="A", limit=10)
            assert result["processed"] == 1, f"should only process folder A: {result}"
        finally:
            server._generate_summary = original


# ─── T-GRAPH ─────────────────────────────────────────────────────────────────

def test_graph_parse_wikilinks():
    content = "See [[Alpha]] and [[Beta|Alias]] and [[Gamma]]."
    links = server._parse_wikilinks(content)
    assert "Alpha" in links, f"Alpha missing: {links}"
    assert "Beta" in links, f"Beta missing: {links}"
    assert "Gamma" in links, f"Gamma missing: {links}"
    assert "Alias" not in links, f"Alias should not be in links: {links}"


def test_graph_parse_wikilinks_empty():
    assert server._parse_wikilinks("no links here") == []


def test_graph_resolve_wikilink():
    with temp_vault() as v:
        write_note(v, "MyNote.md", "content")
        result = server._resolve_wikilink("MyNote")
        assert result is not None, "should resolve MyNote"
        assert result.stem == "MyNote"


def test_graph_resolve_wikilink_case_insensitive():
    with temp_vault() as v:
        write_note(v, "MyNote.md", "content")
        result = server._resolve_wikilink("mynote")
        assert result is not None, "should resolve case-insensitively"


def test_graph_resolve_wikilink_missing():
    with temp_vault() as v:
        result = server._resolve_wikilink("DoesNotExist")
        assert result is None, "missing note should return None"


def test_graph_walk_basic_out():
    with temp_vault() as v:
        write_note(v, "A.md", "# A\n\n[[B]]\n")
        write_note(v, "B.md", "# B\n\n[[C]]\n")
        write_note(v, "C.md", "# C\n\ncontent\n")
        result = server.obsidian_graph_walk("A.md", depth=2, direction="out", include_l0=False)
        assert result["source"] == "A.md", f"source wrong: {result}"
        assert "nodes" in result, f"nodes missing: {result}"
        assert "B.md" in result["nodes"], f"B.md missing from nodes: {result}"
        assert result["nodes"]["B.md"]["degree"] == 1
        assert result["nodes"]["B.md"]["dir"] == "out"
        assert "C.md" in result["nodes"], f"C.md should appear at depth 2: {result}"
        assert result["nodes"]["C.md"]["degree"] == 2


def test_graph_walk_depth_limit():
    with temp_vault() as v:
        write_note(v, "A.md", "[[B]]\n")
        write_note(v, "B.md", "[[C]]\n")
        write_note(v, "C.md", "[[D]]\n")
        write_note(v, "D.md", "content\n")
        result = server.obsidian_graph_walk("A.md", depth=1, direction="out", include_l0=False)
        assert "B.md" in result["nodes"], "B should be at depth 1"
        assert "C.md" not in result["nodes"], "C should not appear beyond depth 1"
        assert "D.md" not in result["nodes"], "D should not appear beyond depth 1"


def test_graph_walk_in_direction():
    with temp_vault() as v:
        write_note(v, "A.md", "content\n")
        write_note(v, "B.md", "links to [[A]]\n")
        write_note(v, "C.md", "also links to [[A]]\n")
        result = server.obsidian_graph_walk("A.md", depth=1, direction="in", include_l0=False)
        nodes = result["nodes"]
        assert "B.md" in nodes, f"B.md should be backlink: {nodes}"
        assert "C.md" in nodes, f"C.md should be backlink: {nodes}"
        assert nodes["B.md"]["dir"] == "in"
        assert nodes["C.md"]["dir"] == "in"


def test_graph_walk_both_direction():
    with temp_vault() as v:
        write_note(v, "A.md", "links to [[B]]\n")
        write_note(v, "B.md", "content\n")
        write_note(v, "C.md", "links to [[A]]\n")
        result = server.obsidian_graph_walk("A.md", depth=1, direction="both", include_l0=False)
        nodes = result["nodes"]
        assert "B.md" in nodes, f"B.md (out) missing: {nodes}"
        assert "C.md" in nodes, f"C.md (in) missing: {nodes}"
        assert nodes["B.md"]["dir"] == "out"
        assert nodes["C.md"]["dir"] == "in"


def test_graph_walk_unresolvable_link():
    with temp_vault() as v:
        write_note(v, "A.md", "[[ExistsNot]] and [[B]]\n")
        write_note(v, "B.md", "content\n")
        result = server.obsidian_graph_walk("A.md", depth=1, direction="out", include_l0=False)
        assert "B.md" in result["nodes"], "resolvable B.md should appear"
        # ExistsNot.md is not in vault, should not appear
        assert not any("ExistsNot" in k for k in result["nodes"]), "unresolvable link should be skipped"


def test_graph_walk_source_not_in_nodes():
    with temp_vault() as v:
        write_note(v, "A.md", "[[B]]\n")
        write_note(v, "B.md", "[[A]]\n")
        result = server.obsidian_graph_walk("A.md", depth=2, direction="both", include_l0=False)
        assert "A.md" not in result["nodes"], "source should not appear in its own nodes"


def test_graph_walk_include_l0():
    with temp_vault() as v:
        write_note(v, "A.md", "[[B]]\n")
        write_note(v, "B.md", "---\nl0: B note summary\n---\n\ncontent\n")
        result = server.obsidian_graph_walk("A.md", depth=1, direction="out", include_l0=True)
        assert result["nodes"]["B.md"]["l0"] == "B note summary"


def test_graph_walk_missing_source():
    with temp_vault() as v:
        try:
            server.obsidian_graph_walk("does_not_exist.md")
            assert False, "should raise ToolError"
        except ToolError:
            pass


# ─── T-MOC ───────────────────────────────────────────────────────────────────

def test_moc_detection_by_stem_keyword():
    with temp_vault() as v:
        p = write_note(v, "Projects MOC.md", "# Projects MOC\n\n")
        assert server._is_moc(p), "stem with MOC should be detected"

        p2 = write_note(v, "Index.md", "# Index\n\n")
        assert server._is_moc(p2), "stem 'Index' should be detected"

        p3 = write_note(v, "Overview.md", "# Overview\n\n")
        assert server._is_moc(p3), "stem 'Overview' should be detected"

        p4 = write_note(v, "Hub.md", "# Hub\n\n")
        assert server._is_moc(p4), "stem 'Hub' should be detected"


def test_moc_detection_by_frontmatter():
    with temp_vault() as v:
        p = write_note(v, "regular_note.md", "---\ntype: moc\n---\n\ncontent\n")
        assert server._is_moc(p), "frontmatter type: moc should be detected"


def test_moc_detection_by_wikilinks():
    with temp_vault() as v:
        # 5+ wikilinks all pointing to same folder
        (v / "Projects").mkdir()
        write_note(v, "Projects/A.md", "content")
        write_note(v, "Projects/B.md", "content")
        write_note(v, "Projects/C.md", "content")
        write_note(v, "Projects/D.md", "content")
        write_note(v, "Projects/E.md", "content")
        p = write_note(v, "Projects/map.md", "[[A]]\n[[B]]\n[[C]]\n[[D]]\n[[E]]\n")
        assert server._is_moc(p), "5 wikilinks in same folder should be detected as MOC"


def test_moc_detection_false():
    with temp_vault() as v:
        p = write_note(v, "regular_note.md", "---\ntags:\n  - python\n---\n\nSome content here.\n")
        assert not server._is_moc(p), "regular note should not be MOC"


def test_moc_build_map():
    with temp_vault() as v:
        write_note(v, "Projects/Projects MOC.md", "# Projects MOC\n\n[[A]]\n[[B]]\n")
        write_note(v, "Projects/A.md", "content")
        write_note(v, "Projects/B.md", "content")
        moc_map = server._build_moc_map()
        assert len(moc_map) >= 1, f"expected MOC map entry: {moc_map}"
        folder_strs = list(moc_map.keys())
        assert any("Projects" in s for s in folder_strs), f"Projects folder not in map: {moc_map}"


def test_moc_intra_group_suppression():
    """Notes in same folder with a MOC, both listed in MOC, should not be linked to each other."""
    with temp_vault() as v:
        # Create folder with MOC listing both A and B
        (v / "Group").mkdir()
        write_note(v, "Group/Group MOC.md", "# Group MOC\n\n[[NoteA]]\n[[NoteB]]\n")
        write_note(v, "Group/NoteA.md", fm(["python", "django"]) + "# A\ncontent about django python")
        write_note(v, "Group/NoteB.md", fm(["python", "django"]) + "# B\ncontent about django python")
        result = server.obsidian_relink(mode="full", min_score=0.01)
        # Group/NoteA.md and Group/NoteB.md both appear in the MOC, should suppress intra-group link
        a_text = (v / "Group" / "NoteA.md").read_text(encoding="utf-8")
        b_text = (v / "Group" / "NoteB.md").read_text(encoding="utf-8")
        # Since both appear in the MOC's wikilinks, neither should link to the other
        assert "[[NoteB]]" not in a_text, f"NoteA should not link NoteB (suppressed by MOC): {a_text}"
        assert "[[NoteA]]" not in b_text, f"NoteB should not link NoteA (suppressed by MOC): {b_text}"


def test_moc_notes_skipped_as_link_targets():
    """MOC notes should be skipped as link targets in full relink."""
    with temp_vault() as v:
        write_note(v, "Projects MOC.md", "# Projects MOC\n\n[[Alpha]]\n[[Beta]]\n")
        write_note(v, "Alpha.md", fm(["python"]) + "# Alpha\ncontent")
        write_note(v, "Beta.md", fm(["python"]) + "# Beta\ncontent")
        result = server.obsidian_relink(mode="full", min_score=0.01)
        a_text = (v / "Alpha.md").read_text(encoding="utf-8")
        # Alpha should not link to the MOC note
        assert "[[Projects MOC]]" not in a_text, f"MOC should not be a link target: {a_text}"


# ─── Runner ──────────────────────────────────────────────────────────────────

TOKEN_TESTS = [
    ("T-TOKEN-01: summary_only returns l0/l1/lc, no content", test_token_summary_only),
    ("T-TOKEN-02: summary_only with missing l0 returns empty strings", test_token_summary_only_missing_l0),
    ("T-TOKEN-03: names_only=True returns flat paths list", test_token_names_only),
    ("T-TOKEN-04: names_only=True non-recursive only top-level", test_token_names_only_non_recursive),
    ("T-TOKEN-05: search tier=l0 returns l0 field not content", test_token_search_tier_l0),
    ("T-TOKEN-06: search tier=full with include_content returns content", test_token_search_tier_full_default),
    ("T-TOKEN-07: find_related returns abbreviated keys p/s/st/l0", test_token_find_related_abbreviated_keys),
    ("T-TOKEN-08: search content_max_chars default is 500", test_token_search_content_max_default),
]

APPEND_TESTS = [
    ("T-APPEND-01: before_section inserts content before matching heading", test_append_before_section_found),
    ("T-APPEND-02: before_section not found falls back to EOF append", test_append_before_section_not_found_fallback),
    ("T-APPEND-03: before_section=None behaves as normal append", test_append_before_section_none_normal),
    ("T-APPEND-04: before_section matching is case-insensitive", test_append_before_section_case_insensitive),
]

BACKFILL_TESTS = [
    ("T-BACKFILL-01: backfill generates l0/l1 for notes without them", test_backfill_generates_summaries),
    ("T-BACKFILL-02: backfill skips notes with fresh summaries", test_backfill_skips_fresh_summaries),
    ("T-BACKFILL-03: backfill overwrites stale summaries", test_backfill_overwrites_stale),
    ("T-BACKFILL-04: backfill respects limit parameter", test_backfill_respects_limit),
    ("T-BACKFILL-05: backfill respects folder scoping", test_backfill_folder_scoping),
]

GRAPH_TESTS = [
    ("T-GRAPH-01: _parse_wikilinks extracts targets and strips aliases", test_graph_parse_wikilinks),
    ("T-GRAPH-02: _parse_wikilinks returns empty for no links", test_graph_parse_wikilinks_empty),
    ("T-GRAPH-03: _resolve_wikilink finds file by stem", test_graph_resolve_wikilink),
    ("T-GRAPH-04: _resolve_wikilink is case-insensitive", test_graph_resolve_wikilink_case_insensitive),
    ("T-GRAPH-05: _resolve_wikilink returns None for missing", test_graph_resolve_wikilink_missing),
    ("T-GRAPH-06: graph_walk BFS out direction", test_graph_walk_basic_out),
    ("T-GRAPH-07: graph_walk depth limit enforced", test_graph_walk_depth_limit),
    ("T-GRAPH-08: graph_walk in direction uses reverse index", test_graph_walk_in_direction),
    ("T-GRAPH-09: graph_walk both direction unions out+in", test_graph_walk_both_direction),
    ("T-GRAPH-10: graph_walk silently skips unresolvable links", test_graph_walk_unresolvable_link),
    ("T-GRAPH-11: graph_walk source does not appear in nodes", test_graph_walk_source_not_in_nodes),
    ("T-GRAPH-12: graph_walk include_l0 reads frontmatter", test_graph_walk_include_l0),
    ("T-GRAPH-13: graph_walk raises ToolError for missing source", test_graph_walk_missing_source),
]

MOC_TESTS = [
    ("T-MOC-01: _is_moc detects MOC/Index/Overview/Hub in stem", test_moc_detection_by_stem_keyword),
    ("T-MOC-02: _is_moc detects frontmatter type: moc", test_moc_detection_by_frontmatter),
    ("T-MOC-03: _is_moc detects 5+ wikilinks all same folder", test_moc_detection_by_wikilinks),
    ("T-MOC-04: _is_moc returns False for regular note", test_moc_detection_false),
    ("T-MOC-05: _build_moc_map returns folder->moc_path dict", test_moc_build_map),
    ("T-MOC-06: intra-group link suppression when both in MOC", test_moc_intra_group_suppression),
    ("T-MOC-07: MOC notes skipped as link targets in full relink", test_moc_notes_skipped_as_link_targets),
]


if __name__ == "__main__":
    print()
    print("=" * 68)
    print("QA TEST SUITE v2 -- Myobscelium v2")
    print("=" * 68)

    print("")
    print("--- T-TOKEN: Token efficiency params ---")
    for name, fn in TOKEN_TESTS:
        run_test(name, fn)

    print("")
    print("--- T-APPEND: before_section param ---")
    for name, fn in APPEND_TESTS:
        run_test(name, fn)

    print("")
    print("--- T-BACKFILL: obsidian_backfill_summaries ---")
    for name, fn in BACKFILL_TESTS:
        run_test(name, fn)

    print("")
    print("--- T-GRAPH: obsidian_graph_walk ---")
    for name, fn in GRAPH_TESTS:
        run_test(name, fn)

    print("")
    print("--- T-MOC: MOC detection and relink suppression ---")
    for name, fn in MOC_TESTS:
        run_test(name, fn)

    passing = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    failing = [(n, d) for n, ok, d in RESULTS if not ok]

    print()
    print("=" * 68)
    print(f"SUMMARY: {passing}/{total} tests passed")
    if failing:
        print(f"FAILED TESTS ({len(failing)}):")
        for name, detail in failing:
            print(f"  [FAIL] {name}")
            for line in detail.splitlines():
                print(f"         {line}")
    print("=" * 68)
    import sys as _sys
    _sys.exit(0 if passing == total else 1)
