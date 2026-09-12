import importlib.util, sys, tempfile, pathlib
sys.path.insert(0, "agents/shared")
spec = importlib.util.spec_from_file_location("rework_shadow", "scripts/rework-shadow.py")
rs = importlib.util.module_from_spec(spec); spec.loader.exec_module(rs)
import uptake_core

BODY = ("the connection pool died because deadpool recycled a socket the server had already "
        "closed and the retry loop kept handing the same broken object back to every caller, "
        "so the fix was to drop the idle timeout below the load balancer's own ") * 3

def build(tmp):
    v = pathlib.Path(tmp) / "wiki"; v.mkdir()
    (v / "wiki-0007.md").write_text(BODY, encoding="utf-8")
    (v / "tiny.md").write_text("short", encoding="utf-8")
    return rs.vault_phrases(str(v))

def test_a_rebuilt_note_is_found_and_a_cited_one_is_not():
    with tempfile.TemporaryDirectory() as tmp:
        by_phrase, names = build(tmp)
        assert "tiny.md" not in names, "a note too short to be distinctive is not indexed"
        ph = uptake_core.phrases(BODY, limit=rs.PHRASES_PER_NOTE)
        rebuilt = "[user] why\n[assistant] " + " ".join(ph[:3]) + "\n"
        assert "wiki-0007.md" in rs.confirmed(rs.matches(rebuilt, by_phrase))
        assert "wiki-0007" in set(uptake_core._words("per wiki-0007 this is it")), "naming is detected"

def test_one_phrase_is_a_shared_subject_not_a_rebuild():
    # Two documents about one subject share a phrase; that is the coincidence the control arm of
    # the scorer exists to price. Only a second distinct phrase makes it a rebuild.
    with tempfile.TemporaryDirectory() as tmp:
        by_phrase, _ = build(tmp)
        one = next(iter(by_phrase))
        hits = rs.matches("[assistant] " + one + "\n", by_phrase)
        assert sum(len(p) for p in hits.values()) == 1, "the fixture must carry exactly one phrase"
        assert rs.confirmed(hits) == {}, "one phrase must not be reported as a rebuild"

def test_a_user_turn_is_never_scanned():
    text = "[user] " + BODY + "\n[assistant] ok\n"
    assert all("deadpool" not in " ".join(w) for w in rs.turns(text)), "only assistant turns count"

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn): fn()
    print("ok - rework-shadow: a rebuilt note is found, a cited one is not")
