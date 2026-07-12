"""Entity resolution: scoring, decision bands, measured precision/recall, undo."""

from __future__ import annotations

from attestari import LexicalEntityResolver, Memory


def test_lexical_resolver_bands_and_canonical() -> None:
    r = LexicalEntityResolver()
    result = r.resolve(["Acme", "Acme Corp", "Globex", "Globex Inc", "Berlin"])
    merged = {(d.canonical, d.alias) for d in result.auto_merges}
    # Qualified forms auto-merge, with the longer name as canonical.
    assert ("Acme Corp", "Acme") in merged
    assert ("Globex Inc", "Globex") in merged
    # Unrelated names are not merged.
    assert all("Berlin" not in (d.canonical, d.alias) for d in result.auto_merges)


def test_resolution_precision_recall() -> None:
    names = ["Acme", "Acme Corp", "Globex", "Globex Inc", "Berlin", "Paris"]
    expected = {frozenset({"Acme", "Acme Corp"}), frozenset({"Globex", "Globex Inc"})}
    result = LexicalEntityResolver().resolve(names)
    got = {frozenset({d.canonical, d.alias}) for d in result.auto_merges}
    tp = len(got & expected)
    precision = tp / len(got) if got else 0.0
    recall = tp / len(expected)
    assert precision == 1.0  # no false merges
    assert recall == 1.0  # both true merges found


def test_merge_then_unmerge_is_reversible() -> None:
    mem = Memory()
    mem.merge_entities("Acme Corp", "Acme", evidence="test")
    assert mem._project().resolve("Acme") == "Acme Corp"
    mem.unmerge_entities("Acme Corp", "Acme")
    # Alias no longer maps to the canonical entity.
    assert mem._project().resolve("Acme") == "Acme"


def test_resolve_entities_auto_merges_in_memory() -> None:
    mem = Memory()
    result = mem.resolve_entities(["Acme", "Acme Corp"], auto=True)
    assert result.auto_merges
    assert mem._project().resolve("Acme") == "Acme Corp"
