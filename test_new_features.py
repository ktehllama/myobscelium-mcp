import sys, os, time, tempfile, shutil
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


def fm(tags):
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    return f"---\ntags:\n{tag_lines}\n---\n\n"


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


# --- T01-T11: _patch_section ---
_NL = chr(10)

def test_01():
    with temp_vault() as v:
        content = (
            "# Title\n\n"
            "## Notes\n\nsome notes\n\n"
            "## Related\n\nold related content\n\n"
            "## References\n\nsome refs\n"
        )
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "Related", "heading", "new related content")
        text = p.read_text(encoding="utf-8")
        assert status == "ok", f"expected ok, got {status!r}"
        assert "## Related\n" in text
        assert "new related content" in text
        assert "old related content" not in text
        assert "## References\n" in text
        assert "some refs" in text
        assert "## Notes\n" in text

def test_02():
    with temp_vault() as v:
        content = "# Title\n\n## Related\n\nsome content\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(
            p, "Related", "heading", "new content",
            heading_level=3, create_if_missing=False
        )
        assert status == "not_found", f"expected not_found, got {status!r}"
        assert "some content" in p.read_text(encoding="utf-8"), "file mutated"

def test_03():
    with temp_vault() as v:
        content = "# Title\n\n### Related\n\nold content\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "Related", "heading", "new content", heading_level=None)
        text = p.read_text(encoding="utf-8")
        assert status == "ok", f"expected ok, got {status!r}"
        assert "new content" in text
        assert "old content" not in text
        assert "### Related" in text

def test_04():
    with temp_vault() as v:
        content = "# Title\n\nBody text.\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "Related", "heading", "link1\nlink2", create_if_missing=True)
        text = p.read_text(encoding="utf-8")
        assert status == "created", f"expected created, got {status!r}"
        assert "## Related\n" in text, "heading not appended"
        assert "link1" in text
        assert "Body text." in text, "original body lost"

def test_05():
    with temp_vault() as v:
        content = "# Title\n\nBody text.\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "Related", "heading", "anything", create_if_missing=False)
        assert status == "not_found", f"expected not_found, got {status!r}"
        assert p.read_text(encoding="utf-8") == content, "file modified despite not_found"

def test_06():
    with temp_vault() as v:
        p = write_note(v, "note.md", "foo bar foo bar")
        status = server._patch_section(p, "foo", "text", "baz")
        assert status == "ok", f"expected ok, got {status!r}"
        text = p.read_text(encoding="utf-8")
        assert text == "baz bar foo bar", f"first-only replace, got: {text!r}"

def test_07():
    with temp_vault() as v:
        content = "hello world\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "missing text", "text", "anything")
        assert status == "not_found", f"expected not_found, got {status!r}"
        assert p.read_text(encoding="utf-8") == content, "file modified on not_found"

def test_08():
    with temp_vault() as v:
        content = "line1\nline2\nline3\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "line1\nline2", "text", "replaced")
        assert status == "ok", f"expected ok, got {status!r}"
        text = p.read_text(encoding="utf-8")
        assert "replaced" in text, "replacement not applied"
        assert "line1" not in text, "matched text not removed"
        assert "line3" in text, "content after match lost"

def test_09():
    with temp_vault() as v:
        content = "some content without newline"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "Related", "heading", "* [[A]]", create_if_missing=True)
        assert status == "created", f"expected created, got {status!r}"
        text = p.read_text(encoding="utf-8")
        assert text.startswith("some content without newline"), "original content lost"
        assert "## Related\n" in text, "## Related heading missing"
        assert "* [[A]]" in text, "content missing"
        orig_end = len("some content without newline")
        heading_start = text.index("## Related")
        sep = text[orig_end:heading_start]
        assert sep == "\n\n", f"expected 2-newline sep, got {sep!r}"

def test_10():
    with temp_vault() as v:
        content = "# Title\n\n## Related\n\nold content\n"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, "Related", "heading", "new content")
        text = p.read_text(encoding="utf-8")
        assert status == "ok", f"expected ok, got {status!r}"
        assert "## Related\n" in text, "heading removed"
        assert "new content" in text, "new content missing"
        assert "old content" not in text, "old content remains"
        headings = [l for l in text.splitlines() if l.startswith("#")]
        assert len(headings) == 2, f"unexpected headings: {headings}"

def test_11():
    with temp_vault() as v:
        literal = "price: 0.00 (sale) [tag] a+b.*"
        content = f"before{_NL}{literal}{_NL}after{_NL}"
        p = write_note(v, "note.md", content)
        status = server._patch_section(p, literal, "text", "REPLACED")
        assert status == "ok", f"expected ok, got {status!r}"
        text = p.read_text(encoding="utf-8")
        assert "REPLACED" in text, "replacement not applied"
        assert literal not in text, "literal match still present"
        assert "before" in text and "after" in text, "surrounding content lost"

# --- T12-T14: obsidian_batch find_related ---

def test_12():
    with temp_vault() as v:
        write_note(v, "A.md", fm(["python", "async"]) + "# A" + _NL + "content")
        write_note(v, "B.md", fm(["python", "async"]) + "# B" + _NL + "content")
        write_note(v, "C.md", fm(["rust"]) + "# C" + _NL + "content")
        result = server.obsidian_batch([
            {"op": "find_related", "path": "A.md"}
        ])
        assert result["success_count"] == 1, f"expected 1 success, got {result}"
        r = result["results"][0]
        assert r["ok"] is True, f"ok=True expected, got {r}"
        assert "related" in r, f"related key missing: {r}"
        assert isinstance(r["related"], list), f"must be list: {r}"
        titles = [x["title"] for x in r["related"]]
        assert "B" in titles, f"B not in {titles}"
        assert "C" not in titles, f"C in {titles}"

def test_13():
    with temp_vault() as v:
        write_note(v, "real.md", fm(["python"]) + "# Real" + _NL + "content")
        result = server.obsidian_batch([
            {"op": "find_related", "path": "does_not_exist.md"},
            {"op": "write", "path": "created.md", "content": "hello"},
        ])
        r0 = result["results"][0]
        assert r0["ok"] is False, f"ok=False expected, got {r0}"
        r1 = result["results"][1]
        assert r1["ok"] is True, f"second op should succeed, got {r1}"
        assert (v / "created.md").exists(), "created.md not written"

def test_14():
    with temp_vault() as v:
        write_note(v, "X.md", fm(["ml", "python"]) + "# X" + _NL + "content")
        write_note(v, "Y.md", fm(["ml", "python"]) + "# Y" + _NL + "content")
        result = server.obsidian_batch([
            {"op": "write", "path": "newfile.md", "content": "brand new"},
            {"op": "find_related", "path": "X.md"},
        ])
        assert result["success_count"] == 2, f"expected 2 successes: {result}"
        assert result["results"][0]["ok"] is True
        assert result["results"][1]["ok"] is True
        assert (v / "newfile.md").exists()
        assert isinstance(result["results"][1]["related"], list)

# --- T15-T20: obsidian_relink ---

def test_15():
    with temp_vault() as v:
        (v / "Chats").mkdir()
        write_note(v, "Chats/old.md", fm(["python", "async"]) + "# Old" + _NL + "content")
        time.sleep(0.06)
        write_note(v, "Chats/new.md", fm(["python", "async"]) + "# New" + _NL + "content")
        write_note(v, "PythonRef.md", fm(["python", "async"]) + "# PythonRef" + _NL + "content")
        result = server.obsidian_relink(mode="normal", min_score=0.01)
        for key in ("mode", "note", "status"):
            assert key in result, f"missing key {key!r}: {result}"
        assert result["mode"] == "normal"
        note_stem = Path(result["note"]).stem
        assert note_stem == "new", f"expected newest note, got {note_stem!r}"

def test_16():
    with temp_vault() as v:
        (v / "Chats").mkdir()
        result = server.obsidian_relink(mode="normal")
        assert result["status"] == "no_notes_found", f"expected no_notes_found: {result}"
        assert result["note"] is None, f"expected note=None: {result}"

def test_17():
    with temp_vault() as v:
        write_note(v, "A.md", fm(["python", "async"]) + "# A" + _NL + "content")
        write_note(v, "B.md", fm(["python", "async"]) + "# B" + _NL + "content")
        result = server.obsidian_relink(mode="full", min_score=0.01)
        for key in ("mode", "updated", "no_change", "no_matches", "errored", "errors"):
            assert key in result, f"missing key {key!r}: {result}"
        assert result["mode"] == "full"
        assert isinstance(result["errors"], list)
        assert isinstance(result["updated"], int)
        assert isinstance(result["no_change"], int)
        assert isinstance(result["no_matches"], int)
        assert isinstance(result["errored"], int)

def test_18():
    with temp_vault() as v:
        write_note(v, "B.md", fm(["python", "ml"]) + "# B" + _NL + "content")
        write_note(v, "A.md", fm(["python", "ml"]) + "# A" + _NL + _NL + "## Related" + _NL + _NL + "* [[B]] -- #python" + _NL)
        a_path = v / "A.md"
        server._apply_relink(a_path, ["* [[B]] -- #python #ml"])
        txt = a_path.read_text(encoding="utf-8")
        b_count = txt.count("[[B]]")
        assert b_count == 1, f"[[B]] count={b_count} (dedup failed)"
    with temp_vault() as v:
        write_note(v, "B.md", fm(["python"]) + "# B" + _NL + "content")
        write_note(v, "C.md", fm(["python"]) + "# C" + _NL + "content")
        write_note(v, "A.md", fm(["python"]) + "# A" + _NL + _NL + "## Related" + _NL + _NL + "* [[B]] -- #python" + _NL)
        a_path = v / "A.md"
        status = server._apply_relink(a_path, ["* [[C]] -- #python"])
        txt = a_path.read_text(encoding="utf-8")
        assert "[[B]]" in txt, "existing [[B]] was removed"
        assert "[[C]]" in txt, "new [[C]] was not added"
        assert status == "updated", f"expected updated, got {status!r}"

def test_19():
    r = server._passes_tag_filter(["chat", "claude"], "NoteA", "NoteB")
    assert r is False, f"Expected False for generic-only tags, got {r}"
    r2 = server._passes_tag_filter(["ai", "conversation"], "Alpha", "Beta")
    assert r2 is False, f"Expected False for generic-only, got {r2}"

def test_20():
    r = server._passes_tag_filter(["chat", "python"], "Alpha", "Beta")
    assert r is True, f"Expected True for non-generic python, got {r}"
    r2 = server._passes_tag_filter(["chat"], "python-notes", "python-guide")
    assert r2 is True, f"Expected True via title word overlap, got {r2}"

def test_dead_link_cleanup():
    with temp_vault() as v:
        write_note(v, "A.md", fm(["python"]) + "# A" + _NL + _NL + "## Related" + _NL + _NL + "* [[Ghost]] -- #python" + _NL)
        write_note(v, "B.md", fm(["python"]) + "# B" + _NL + "content")
        a_path = v / "A.md"
        status = server._apply_relink(a_path, ["* [[B]] -- #python"])
        txt = a_path.read_text(encoding="utf-8")
        assert "[[Ghost]]" not in txt, "dead link [[Ghost]] should be removed"
        assert "[[B]]" in txt, "new valid [[B]] should be added"
        assert status == "updated", f"expected updated, got {status!r}"

def test_bidirectional_relink():
    with temp_vault() as v:
        write_note(v, "Alpha.md", fm(["django", "python"]) + "# Alpha" + _NL + "content")
        write_note(v, "Beta.md", fm(["django", "python"]) + "# Beta" + _NL + "content")
        server.obsidian_relink(mode="full", min_score=0.01)
        a_txt = (v / "Alpha.md").read_text(encoding="utf-8")
        b_txt = (v / "Beta.md").read_text(encoding="utf-8")
        a_links_b = "[[Beta]]" in a_txt
        b_links_a = "[[Alpha]]" in b_txt
        # Exactly one direction should exist — no artificial fat nodes
        assert a_links_b != b_links_a, (
            f"Expected exactly one direction: Alpha→Beta={a_links_b}, Beta→Alpha={b_links_a}"
        )


TESTS = [
    ("T01: heading mode replaces body, preserves heading + sections below", test_01),
    ("T02: heading_level=3 does NOT match ## heading (level 2)", test_02),
    ("T03: heading_level=None matches any level", test_03),
    ("T04: create_if_missing=True appends new section when absent", test_04),
    ("T05: create_if_missing=False returns not_found, file unchanged", test_05),
    ("T06: text mode replaces first occurrence only", test_06),
    ("T07: text mode returns not_found when text absent", test_07),
    ("T08: text mode handles multiline match", test_08),
    ("T09: no trailing newline -- sep is exactly 2 newlines", test_09),
    ("T10: target is last section (no following heading)", test_10),
    ("T11: regex metacharacters in text match are treated as literal", test_11),
    ("T12: batch find_related returns ok=True and related list", test_12),
    ("T13: batch find_related for missing note => ok=False, no abort", test_13),
    ("T14: batch mix of write + find_related -- both complete", test_14),
    ("T15: relink normal picks most-recently-modified note, correct keys", test_15),
    ("T16: relink normal returns no_notes_found when Chats/ empty", test_16),
    ("T17: relink full mode returns all required summary keys", test_17),
    ("T18: merge -- existing links preserved, new added, no duplicates", test_18),
    ("T19: tag filter excludes notes sharing only generic tags", test_19),
    ("T20: tag filter includes notes with at least one non-generic tag", test_20),
    ("EXTRA: dead link cleanup removes wikilinks to non-existent notes", test_dead_link_cleanup),
    ("EXTRA: full mode applies bidirectional links", test_bidirectional_relink),
]


if __name__ == "__main__":
    print()
    print("=" * 68)
    print("QA TEST SUITE -- obsidian_claude_mcp new features")
    print("=" * 68)

    print("")
    print("--- _patch_section (T01-T11) ---")
    for name, fn in TESTS[:11]:
        run_test(name, fn)

    print("")
    print("--- obsidian_batch find_related op (T12-T14) ---")
    for name, fn in TESTS[11:14]:
        run_test(name, fn)

    print("")
    print("--- obsidian_relink (T15-T20) ---")
    for name, fn in TESTS[14:20]:
        run_test(name, fn)

    print("")
    print("--- Additional edge cases ---")
    for name, fn in TESTS[20:]:
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
