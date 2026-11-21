"""merge_manifests: the union of one stack's manifests (#238 Slice 5)."""

from __future__ import annotations

import pytest

from infra2_sdk.runtime import EnvironmentField, EnvironmentManifest, merge_manifests


def _m(source: str, *fields: EnvironmentField) -> EnvironmentManifest:
    return EnvironmentManifest(source=source, fields=fields)


def _f(env: str, **kw) -> EnvironmentField:
    return EnvironmentField(field=env.lower(), env=env, **kw)


def test_the_union_keeps_order_and_the_first_declaration() -> None:
    llm = _m("llm", _f("DATABASE_URL", source="runtime", required=True, description="llm"), _f("A"))
    engine = _m("engine", _f("DATABASE_URL", source="runtime", description="engine"), _f("B"))
    merged = merge_manifests([("llm.json", llm), ("engine.json", engine)], source="truealpha/app")
    assert merged.source == "truealpha/app"
    assert [f.env for f in merged.fields] == ["DATABASE_URL", "A", "B"]
    first = merged.fields[0]
    assert first.required is True and first.description == "llm"


@pytest.mark.parametrize(
    ("key", "a", "b"),
    [
        ("source", "runtime", "human"),
        ("store_key", "x", "y"),
        ("empty_ok", False, True),
        ("scope", "env", "project"),
    ],
)
def test_a_render_key_that_differs_raises_and_names_the_manifest(key, a, b) -> None:
    base = {"source": "runtime", key: a}
    one = _m("one", _f("DATABASE_URL", **base))
    two = _m("two", _f("DATABASE_URL", **{**base, key: b}))
    with pytest.raises(
        ValueError, match=rf"stack: DATABASE_URL.{key} declared differently in two.json"
    ):
        merge_manifests([("one.json", one), ("two.json", two)], source="stack")


def test_excluded_variables_are_dropped() -> None:
    one = _m("one", _f("A"), _f("B"))
    merged = merge_manifests([("one.json", one)], source="s", exclude={"B"})
    assert [f.env for f in merged.fields] == ["A"]
