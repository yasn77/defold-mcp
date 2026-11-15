"""Hermetic tests for the SQLite FTS5 documentation & asset portal indexer.

All fixtures are in-memory: no network, no filesystem outside pytest's
``tmp_path``, and every ``httpx.Client`` is a ``MockTransport`` client.
"""

import io
import json
import tarfile
import zipfile

import httpx
import pytest

from defold_mcp import config
from defold_mcp.docs_index import (
    CODELOAD_TARBALL_URL,
    DocsIndex,
)

# ---------------------------------------------------------------------------
# Fixtures & builders
# ---------------------------------------------------------------------------

ADDRESSING_MD = (
    "# Addressing\n\n"
    "Defold uses a simple addressing scheme to locate resources.\n"
    "Use `resource:/main/main.collection` style paths.\n"
)

RAYCAST_MD = (
    "# Raycast\n\n"
    "Physics raycast example using the physics module.\n"
    "Call `physics.raycast` to probe the world.\n"
)

# REAL ref-doc shape: each ``*_doc.json`` is a NAMESPACE document.
GO_DOC = {
    "info": {"namespace": "go", "language": "lua"},
    "elements": [
        {
            "name": "go.animate",
            "signature": (
                "go.animate(url, property, playback, to, easing, duration, "
                "[delay], [on_completion])"
            ),
            "description": (
                "Tweens (animates) a named property on a game object or "
                "component."
            ),
            "parameters": [
                {"name": "url", "doc": "the game object or component to animate"},
                {"name": "property", "doc": "the property to animate"},
            ],
            "examples": [
                "local anim = go.animate('/hero#sprite', 'scale', "
                "go.PLAYBACK_LOOP_PINGPONG, 2.0, go.EASING_LINEAR, 1.0)"
            ],
        },
        {
            "name": "go.get",
            "signature": "go.get(url, property)",
            "description": (
                "Gets the value of a property on a game object or component."
            ),
            "parameters": [
                {"name": "url", "doc": "the game object or component"},
                {"name": "property", "doc": "the property to read"},
            ],
            "examples": ["local pos = go.get('/hero#sprite', 'position')"],
        },
    ],
}

# REAL Asset Portal feed shape: {"items": [...]}.
ASSET_FEED = {
    "items": [
        {
            "id": "monarch",
            "title": "Monarch",
            "author": "britzl",
            "description": "Screen manager - flows and popups",
            "tags": ["gui"],
            "stars": 100,
            "api": "https://raw.githubusercontent.com/britzl/monarch/master/API.md",
            "example_code": "local monarch = require('monarch')",
            "content": [
                "...history...",
                "https://github.com/britzl/monarch/archive/refs/tags/v1.0.zip",
            ],
        },
        {
            "id": "tiny",
            "title": "Tiny",
            "author": "example",
            "description": "A minimal screen manager",
            "tags": ["gui"],
            "stars": 5,
            "api": "https://raw.githubusercontent.com/example/tiny/master/API.md",
            "content": [
                "https://github.com/example/tiny/archive/refs/tags/v0.1.zip",
            ],
        },
        {
            "id": "scene3d",
            "title": "Scene3D",
            "description": "A 3d screen manager (deprecated)",
            "stars": 50,
            "unlisted": False,
        },
        {
            "id": "hidden_dep",
            "title": "Hidden",
            "description": "An unlisted screen manager",
            "stars": 999,
            "unlisted": True,
        },
    ]
}


def make_refdoc_zip(entries: dict[str, dict]) -> bytes:
    """Build an in-memory ref-doc.zip with ``*_doc.json`` members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, json.dumps(payload).encode("utf-8"))
    return buf.getvalue()


def make_tarball(entries: dict[str, str]) -> bytes:
    """Build an in-memory codeload-style tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in entries.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def index() -> DocsIndex:
    idx = DocsIndex(":memory:")
    yield idx
    idx.close()


# ---------------------------------------------------------------------------
# 1. Indexing populates the pinned FTS5 schema
# ---------------------------------------------------------------------------


def test_indexing_populates_docs_fts(index: DocsIndex):
    index.ingest_manual("addressing", "Addressing", ADDRESSING_MD)
    index.ingest_refdoc(make_refdoc_zip({"go_doc.json": GO_DOC}))
    index.ingest_example("physics/raycast", "Raycast", RAYCAST_MD)
    index.ingest_assets(ASSET_FEED)

    # Pinned schema: FTS5 virtual table with porter unicode61 tokenizer.
    schema = index.schema_sql()
    assert "fts5" in schema
    assert "porter unicode61" in schema

    # Every ingested source produced rows (the two excluded assets —
    # ``scene3d`` and the unlisted entry — are never indexed).
    assert index.stats() == {
        "manuals": 1,
        "lua_api": 2,
        "examples": 1,
        "assets": 2,
    }

    doc = index.get_doc("lua_api:go.animate")
    assert doc["title"] == "go.animate"
    assert doc["namespace"] == "go"
    assert "Tweens (animates) a named property" in doc["content"]
    assert "go.animate(url, property" in doc["content"]
    assert "`url`" in doc["content"]


# ---------------------------------------------------------------------------
# 2. Exact qualified-name title boost in lua_api search
# ---------------------------------------------------------------------------


def test_search_lua_api_title_boost(index: DocsIndex):
    index.ingest_api_markdown(
        "go.animate", "go.animate", "Animate a property of a component over time."
    )
    index.ingest_api_markdown(
        "go.animate_rotation",
        "go.animate_rotation",
        "Animate the rotation of a component over time.",
    )
    index.ingest_api_markdown(
        "go.get", "go.get", "Get the value of a property on a component."
    )

    results = index.search("go.animate", scope="lua_api")
    assert results, "expected at least one hit for 'go.animate'"
    assert results[0]["doc_id"] == "lua_api:go.animate"
    # Both animate docs match the phrase; the exact title must lead.
    titles = [r["title"] for r in results]
    assert "go.animate_rotation" in titles
    assert "go.get" not in titles


# ---------------------------------------------------------------------------
# 3. Asset ranking by stars + ingestion exclusions
# ---------------------------------------------------------------------------


def test_assets_ranking_and_exclusions(index: DocsIndex):
    index.ingest_assets(ASSET_FEED)

    results = index.search("screen manager", scope="assets")
    ids = [r["doc_id"] for r in results]
    # High-star monarch must outrank low-star tiny.
    assert ids[0] == "assets:monarch"
    assert "assets:tiny" in ids
    assert ids.index("assets:monarch") < ids.index("assets:tiny")

    # Excluded assets must not be indexed at all (not merely unranked).
    for excluded in ("assets:scene3d", "assets:hidden_dep"):
        row = index._conn.execute(
            "SELECT count(*) FROM docs_fts WHERE doc_id = ?", (excluded,)
        ).fetchone()
        assert row[0] == 0, f"{excluded} should not be indexed"


# ---------------------------------------------------------------------------
# 4. get_doc pagination and the 25,000-char cap
# ---------------------------------------------------------------------------


def test_get_doc_pagination(index: DocsIndex):
    body = "word " * 12000  # 60,000 chars
    index.ingest_manual("long", "Long", body)

    first = index.get_doc("manuals:long")
    assert first["total_len"] == 60000
    assert first["offset"] == 0
    assert len(first["content"]) == 25000
    assert first["truncated"] is True

    second = index.get_doc("manuals:long", offset=25000)
    assert len(second["content"]) == 25000
    assert second["truncated"] is True

    last = index.get_doc("manuals:long", offset=50000)
    assert len(last["content"]) == 10000
    assert last["truncated"] is False

    # Reassembly must reproduce the original body.
    joined = first["content"] + second["content"] + last["content"]
    assert joined == body

    with pytest.raises(KeyError):
        index.get_doc("manuals:does-not-exist")


# ---------------------------------------------------------------------------
# 5. Stable doc_id across re-ingestion (replace, never duplicate)
# ---------------------------------------------------------------------------


def test_stable_doc_id_reingestion(index: DocsIndex):
    index.ingest_manual("addressing", "Addressing", ADDRESSING_MD)
    index.ingest_manual(
        "addressing", "Addressing", "# Addressing\n\nRevised body text.\n"
    )

    assert index.stats()["manuals"] == 1
    doc = index.get_doc("manuals:addressing")
    assert "Revised body text." in doc["content"]
    assert "simple addressing scheme" not in doc["content"]


# ---------------------------------------------------------------------------
# 6. Scope heuristic for llms/apis/<slug>.md
# ---------------------------------------------------------------------------


def test_api_scope_heuristic(index: DocsIndex):
    index.ingest_api_markdown("admob", "admob", "AdMob extension.")
    index.ingest_api_markdown("dmGameObject", "dmGameObject", "C++ class.")
    index.ingest_api_markdown("DmSound", "DmSound", "C++ class.")
    index.ingest_api_markdown("go.animate", "go.animate", "Lua function.")

    assert index.stats() == {
        "ext_api": 1,
        "cpp_api": 2,
        "lua_api": 1,
    }
    assert index.get_doc("ext_api:admob")["title"] == "admob"
    assert index.get_doc("cpp_api:dmGameObject")["title"] == "dmGameObject"
    assert index.get_doc("cpp_api:DmSound")["title"] == "DmSound"
    assert index.get_doc("lua_api:go.animate")["title"] == "go.animate"


# ---------------------------------------------------------------------------
# 7. Unknown scope raises ValueError
# ---------------------------------------------------------------------------


def test_search_unknown_scope_raises(index: DocsIndex):
    index.ingest_manual("addressing", "Addressing", ADDRESSING_MD)
    with pytest.raises(ValueError):
        index.search("addressing", scope="bogus_scope")


# ---------------------------------------------------------------------------
# 8. Streaming llms tarball ingestion (only */llms/* members)
# ---------------------------------------------------------------------------


def test_llms_tarball_ingestion(index: DocsIndex):
    tarball = make_tarball(
        {
            "defold.github.io-master/llms/manuals/addressing.md": ADDRESSING_MD,
            "defold.github.io-master/llms/apis/go.md": "# go\n\nThe go module.\n",
            "defold.github.io-master/llms/apis/go.animate.md": (
                "# go.animate\n\nAnimate a property.\n"
            ),
            "defold.github.io-master/llms/apis/dmGameObject.md": (
                "# dmGameObject\n\nC++ class.\n"
            ),
            "defold.github.io-master/llms/apis/admob.md": (
                "# admob\n\nAdMob extension.\n"
            ),
            "defold.github.io-master/llms/examples/physics/raycast.md": RAYCAST_MD,
            "defold.github.io-master/llms/apis/../evil.md": "# evil\n\nEvil.\n",
            "defold.github.io-master/src/game.project": "name = test\n",
        }
    )

    index.ingest_llms_tarball(tarball)

    assert index.stats() == {
        "manuals": 1,
        "lua_api": 2,
        "cpp_api": 1,
        "ext_api": 1,
        "examples": 1,
    }

    # The non-llms member must never be indexed.
    row = index._conn.execute(
        "SELECT count(*) FROM docs_fts WHERE doc_id LIKE '%game.project%'"
    ).fetchone()
    assert row[0] == 0

    # A member whose slug contains a `..` component would escape the
    # llms/ tree; it must be skipped entirely.
    row = index._conn.execute(
        "SELECT count(*) FROM docs_fts WHERE doc_id LIKE '%evil%'"
    ).fetchone()
    assert row[0] == 0

    # Titles are derived from the Markdown heading.
    assert index.get_doc("manuals:addressing")["title"] == "Addressing"
    assert index.get_doc("examples:physics/raycast")["title"] == "Raycast"


# ---------------------------------------------------------------------------
# 9. fetch_* helpers with an injectable (mocked) httpx.Client
# ---------------------------------------------------------------------------


def test_fetch_helpers_injectable_client(index: DocsIndex):
    tarball = make_tarball(
        {"defold.github.io-master/llms/manuals/addressing.md": ADDRESSING_MD}
    )
    refdoc_bytes = b"PK\x03\x04fake-zip"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("dependencies_store.json"):
            return httpx.Response(200, json=ASSET_FEED)
        if "ref-doc.zip" in request.url.path:
            return httpx.Response(200, content=refdoc_bytes)
        return httpx.Response(200, content=tarball)

    client = mock_client(handler)
    try:
        assert index.fetch_llms_tarball(client=client) == tarball
        assert index.fetch_asset_store(client=client) == ASSET_FEED
        assert index.fetch_refdoc_zip("abc123", client=client) == refdoc_bytes
    finally:
        client.close()

    assert seen == [
        CODELOAD_TARBALL_URL,
        config.ASSET_STORE_JSON_URL,
        f"{config.D_DEFOLD_BASE}/archive/abc123/engine/share/ref-doc.zip",
    ]


# ---------------------------------------------------------------------------
# 10. Regression: REAL ref-doc namespace-document shape, end-to-end
# ---------------------------------------------------------------------------


def test_refdoc_real_namespace_document_shape(index: DocsIndex):
    """Feed the exact namespace-document JSON as a raw string.

    Each ``*_doc.json`` in the real ref-doc zip is a namespace document
    (``info`` + ``elements``), NOT a flat element. Both elements must be
    indexed under ``lua_api`` and the exact qualified name must rank first.
    """
    payload = json.dumps(GO_DOC)  # str, exactly as it would arrive parsed
    count = index.ingest_refdoc(payload)
    assert count == 2

    assert index.stats()["lua_api"] == 2
    results = index.search("go.animate", scope="lua_api")
    assert results, "expected a hit for 'go.animate'"
    assert results[0]["title"] == "go.animate"

    # The second element of the same namespace document is indexed too.
    doc = index.get_doc("lua_api:go.get")
    assert doc["namespace"] == "go"
    assert "go.get(url, property)" in doc["content"]


# ---------------------------------------------------------------------------
# 11. Regression: REAL Asset Portal feed shape, end-to-end
# ---------------------------------------------------------------------------


def test_assets_real_feed_shape(index: DocsIndex):
    """Feed the exact ``{"items": [...]}`` feed as raw JSON bytes.

    The real ``dependencies_store.json`` is a feed with an ``items`` list,
    NOT a flat ``{id: meta}`` map. Ranking and exclusion must hold.
    """
    payload = json.dumps(ASSET_FEED).encode("utf-8")
    count = index.ingest_assets(payload)
    assert count == 2  # monarch + tiny; scene3d and unlisted excluded

    stats = index.stats()
    assert stats["assets"] == 2

    results = index.search("screen manager", scope="assets")
    ids = [r["doc_id"] for r in results]
    assert ids[0] == "assets:monarch"
    assert "assets:tiny" in ids
    assert ids.index("assets:monarch") < ids.index("assets:tiny")

    # url is the last entry of ``content`` (the latest zip).
    monarch = next(r for r in results if r["doc_id"] == "assets:monarch")
    assert monarch["title"] == "Monarch"
    assert monarch["url"].endswith("/monarch/archive/refs/tags/v1.0.zip")
    assert monarch["stars"] == "100"

    doc = index.get_doc("assets:monarch")
    assert doc["title"] == "Monarch"
    assert "Screen manager - flows and popups" in doc["content"]
    assert "local monarch = require('monarch')" in doc["content"]

    # Excluded assets must not be indexed at all.
    for excluded in ("assets:scene3d", "assets:hidden_dep"):
        row = index._conn.execute(
            "SELECT count(*) FROM docs_fts WHERE doc_id = ?", (excluded,)
        ).fetchone()
        assert row[0] == 0, f"{excluded} should not be indexed"


# ---------------------------------------------------------------------------
# 12. ingest_assets rejects feeds without an "items" list
# ---------------------------------------------------------------------------


def test_ingest_assets_flat_map_rejected(index: DocsIndex):
    # The flat {id: meta} map is not a real feed shape and is rejected.
    with pytest.raises(ValueError, match="items"):
        index.ingest_assets({"monarch": {"title": "Monarch", "stars": 1}})
    assert index.stats() == {}


# ---------------------------------------------------------------------------
# 13. get_doc clamps a negative offset and a zero limit
# ---------------------------------------------------------------------------


def test_get_doc_clamps_negative_offset_and_zero_limit(index: DocsIndex):
    index.ingest_manual("short", "Short", "hello world\n")

    doc = index.get_doc("manuals:short", offset=-50)
    assert doc["offset"] == 0
    assert doc["content"] == "hello world\n"

    doc2 = index.get_doc("manuals:short", limit=0)
    assert len(doc2["content"]) == 1
    assert doc2["content"] == "h"


# ---------------------------------------------------------------------------
# 14. Porter stemming: a stemmed query matches a differently-inflected body
# ---------------------------------------------------------------------------


def test_search_porter_stemming(index: DocsIndex):
    index.ingest_manual(
        "animate", "Animate", "# Animate\n\nHow to Animate a property over time.\n"
    )

    results = index.search("animating", scope="manuals")
    assert results, "expected 'animating' to match a document containing 'Animate'"
    assert results[0]["doc_id"] == "manuals:animate"
