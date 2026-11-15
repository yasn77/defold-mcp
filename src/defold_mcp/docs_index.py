"""SQLite FTS5 documentation & asset portal indexer.

Indexes Defold's ``llms/`` tree (manuals, APIs, examples) from the codeload
tarball, version-pinned ``ref-doc.zip`` API metadata, and Insality's
``dependencies_store.json`` into a single FTS5 virtual table for fast,
offline, BM25-ranked search.

The schema is pinned: ``docs_fts(doc_id UNINDEXED, scope UNINDEXED, title,
namespace UNINDEXED, tags UNINDEXED, summary, body, stars UNINDEXED,
url UNINDEXED)`` with the ``porter unicode61`` tokenizer. ``doc_id`` is
deterministic (``<scope>:<source-identity>``) so re-ingestion replaces
documents instead of duplicating them.

FTS5 constraints honoured throughout (verified against sqlite3 3.53.1):

* ``MATCH``, ``bm25()`` and ``rowid`` require the real table name
  (``docs_fts``), never a table alias.
* ``MATCH`` cannot appear inside a ``CASE WHEN``; the title boost therefore
  uses an ``EXISTS`` subquery keyed on ``rowid``.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import tarfile
import zipfile
from typing import Any

import httpx

from . import config

# Codeload tarball of the Defold documentation site (``llms/`` lives at the
# repo root); streaming extraction needs no ``git`` subprocess.
CODELOAD_TARBALL_URL = (
    "https://codeload.github.com/defold/defold.github.io/tar.gz/refs/heads/master"
)

# Scopes accepted by :meth:`DocsIndex.search`.
VALID_SCOPES = (
    "manuals",
    "lua_api",
    "cpp_api",
    "ext_api",
    "examples",
    "assets",
)

# Third-party extension slugs under ``llms/apis/`` (heuristic: a slug in this
# tuple is an extension API, not a built-in Lua module).
KNOWN_EXTENSIONS = (
    "admob",
    "applovin",
    "iap",
    "poki_sdk",
    "crazygames",
    "steam",
    "rive",
    "spine",
    "websocket",
    "extension_adinfo",
    "extension_adpf",
    "extension_review",
    "facebook",
    "fbinstant",
    "googleplaygames",
    "yandex",
    "kongregart",
    "extension_push",
    "extension_webview",
    "extension_window",
)

# Assets never indexed, regardless of listing status.
EXCLUDED_ASSET_IDS = frozenset({"scene3d"})

# Default page size for :meth:`DocsIndex.get_doc`, in characters.
DEFAULT_DOC_LIMIT = 25_000

_SCHEMA_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
    "doc_id UNINDEXED, scope UNINDEXED, title, namespace UNINDEXED, "
    "tags UNINDEXED, summary, body, stars UNINDEXED, url UNINDEXED, "
    "tokenize='porter unicode61')"
)

# ``*/llms/<kind>/<slug>.md`` — the only tarball members we consume.
_LLM_MEMBER_RE = re.compile(
    r"^.*?/llms/(?P<kind>manuals|apis|examples)/(?P<slug>.+)\.md$"
)

_SEARCH_SQL = (
    "SELECT doc_id, scope, title, namespace, summary, stars, url "
    "FROM ("
    "  SELECT doc_id, scope, title, namespace, summary, stars, url, "
    "    bm25(docs_fts) - CASE WHEN EXISTS ("
    "      SELECT 1 FROM docs_fts t "
    "      WHERE t.rowid = docs_fts.rowid AND t.title MATCH ?"
    "    ) THEN 1.0 ELSE 0.0 END AS rank "
    "  FROM docs_fts WHERE docs_fts MATCH ? AND scope = ?"
    ") ORDER BY rank LIMIT ?"
)

_ASSETS_SEARCH_SQL = (
    "SELECT doc_id, scope, title, namespace, summary, stars, url "
    "FROM docs_fts WHERE docs_fts MATCH ? AND scope = 'assets' "
    "ORDER BY CAST(stars AS INTEGER) DESC, bm25(docs_fts) LIMIT ?"
)


def _escape_fts_query(query: str) -> str:
    """Quote ``query`` as a single FTS5 phrase.

    Passing user text as a quoted phrase both prevents FTS5 syntax errors
    (``AND``/``OR``/``*`` in the input) and keeps the match a phrase rather
    than an implicit OR of tokens.
    """
    return '"' + query.replace('"', '""') + '"'


def _title_from_markdown(text: str, fallback: str) -> str:
    """First ``# heading`` line of a Markdown document, else ``fallback``."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _first_line(text: Any) -> str:
    """Return the first non-empty line of ``text`` (used for summaries)."""
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _format_refdoc(element: dict) -> str:
    """Render a ref-doc element as signature + description + parameters + examples."""
    lines = [f"## {element.get('name', '')}", ""]
    if element.get("signature"):
        lines += [f"`{element['signature']}`", ""]
    if element.get("description"):
        lines += [str(element["description"]), ""]
    params = element.get("parameters") or []
    if params:
        lines.append("### Parameters")
        for param in params:
            name = param.get("name", "")
            doc = param.get("doc") or param.get("description") or ""
            lines.append(f"- `{name}`: {doc}")
        lines.append("")
    examples = element.get("examples") or []
    if examples:
        lines.append("### Examples")
        lines.append("```lua")
        lines += [str(ex) for ex in examples]
        lines.append("```")
    return "\n".join(lines)


def _expand_one(payload: Any) -> list[tuple[str, str, dict]]:
    """Expand a single dict into ``(namespace, language, element)`` tuples.

    A dict with an ``elements`` (or ``info``) key is a *namespace document*;
    any other dict is treated as a bare element (namespace from its name).
    """
    if not isinstance(payload, dict):
        return []
    if "elements" in payload or "info" in payload:
        info = payload.get("info") or {}
        namespace = str(info.get("namespace", ""))
        language = str(info.get("language", "lua"))
        out: list[tuple[str, str, dict]] = []
        for element in payload.get("elements") or []:
            if isinstance(element, dict):
                out.append((namespace, language, element))
        return out
    namespace = str(payload.get("name", "")).split(".")[0]
    return [(namespace, "lua", payload)]


def _expand_refdoc_entries(payload: Any) -> list[tuple[str, str, dict]]:
    """Expand a namespace document, element, or list of either into tuples."""
    if isinstance(payload, list):
        out: list[tuple[str, str, dict]] = []
        for item in payload:
            out.extend(_expand_one(item))
        return out
    return _expand_one(payload)


def _parse_refdoc(payload: bytes | str | dict | list) -> list[tuple[str, str, dict]]:
    """Normalise a ref-doc payload into ``(namespace, language, element)`` tuples.

    Accepts a parsed namespace-document dict, a JSON string/bytes of one, the
    whole ``ref-doc.zip`` bytes (each ``*_doc.json`` member is a namespace
    document), or a bare list of element dicts.
    """
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        if zipfile.is_zipfile(io.BytesIO(raw)):
            entries: list[dict] = []
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for name in zf.namelist():
                    if name.endswith("_doc.json"):
                        try:
                            entries.append(json.loads(zf.read(name)))
                        except ValueError:
                            continue
            return _expand_refdoc_entries(entries)
        payload = json.loads(raw)
    if isinstance(payload, str):
        payload = json.loads(payload)
    return _expand_refdoc_entries(payload)


def _asset_body(item: dict) -> str:
    """Build the asset body: description + optional API + example code."""
    parts = [str(item.get("description", ""))]
    if item.get("api"):
        parts.append(f"## API\n\n{item['api']}")
    if item.get("example_code"):
        parts.append(
            "## Example\n\n```lua\n" + str(item["example_code"]) + "\n```"
        )
    return "\n\n".join(part for part in parts if part)


def _asset_url(item: dict) -> str:
    """Latest zip (last ``content`` entry), else the ``api`` link, else ''."""
    content = item.get("content")
    if isinstance(content, list) and content:
        return str(content[-1])
    if item.get("api"):
        return str(item["api"])
    return ""


class DocsIndex:
    """In-memory or on-disk FTS5 index over Defold docs and assets.

    ``db_path`` may be ``":memory:"`` for tests or a filesystem path such as
    ``~/.cache/defold-mcp/docs.db`` for the live server.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(_SCHEMA_SQL)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def schema_sql(self) -> str:
        """Return the pinned ``CREATE VIRTUAL TABLE`` statement for ``docs_fts``."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'docs_fts'"
        ).fetchone()
        return row[0] if row else ""

    # -- ingestion ----------------------------------------------------------

    def _upsert(self, doc_id: str, scope: str, title: str, namespace: str,
                summary: str, body: str, stars: str, url: str,
                tags: str = "") -> None:
        """Insert or replace the document with ``doc_id`` (stable identity)."""
        self._conn.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))
        self._conn.execute(
            "INSERT INTO docs_fts "
            "(doc_id, scope, title, namespace, tags, summary, body, stars, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, scope, title, namespace, tags, summary, body, stars, url),
        )
        self._conn.commit()

    def ingest_manual(self, slug: str, title: str, body: str,
                      url: str = "") -> None:
        """Index one ``llms/manuals/<slug>.md`` document."""
        self._upsert(f"manuals:{slug}", "manuals", title, "", "", body, "0", url)

    def ingest_api_markdown(self, slug: str, title: str, body: str,
                            url: str = "") -> None:
        """Index one ``llms/apis/<slug>.md`` document using the scope heuristic."""
        scope = self.api_scope_for(slug)
        self._upsert(f"{scope}:{slug}", scope, title, "", "", body, "0", url)

    def ingest_example(self, slug: str, title: str, body: str,
                       url: str = "") -> None:
        """Index one ``llms/examples/<slug>.md`` document."""
        self._upsert(f"examples:{slug}", "examples", title, "", "", body, "0", url)

    def ingest_refdoc(self, payload: bytes | str | dict | list) -> int:
        """Index ref-doc API metadata.

        Each ``*_doc.json`` in the real ref-doc zip is a *namespace* document
        (``info`` + ``elements``), not a flat element. Accepts a parsed
        namespace-document dict, a JSON string/bytes of one, the whole
        ``ref-doc.zip`` bytes, or a bare list of element dicts. Each element
        becomes one document (``lua_api`` by default; ``cpp_api`` when the
        namespace language is C++). Returns the number of element docs indexed.
        """
        count = 0
        for namespace, language, element in _parse_refdoc(payload):
            name = element.get("name", "")
            if not name:
                continue
            scope = "cpp_api" if language.lower() in ("cpp", "c++") else "lua_api"
            summary = _first_line(element.get("description", ""))
            self._upsert(
                f"{scope}:{name}", scope, name, namespace,
                summary, _format_refdoc(element),
                "0", "",
            )
            count += 1
        return count

    def ingest_assets(self, store: dict | bytes | str) -> int:
        """Index the Insality ``dependencies_store.json`` feed.

        The feed is ``{"items": [...]}``; each item carries ``id``,
        ``title``, ``description``, ``tags``, ``stars``, ``api``,
        ``example_code`` and ``content``. A dict without an ``items`` list
        raises :class:`ValueError`. Items with ``unlisted == true`` or an id
        in :data:`EXCLUDED_ASSET_IDS` are silently skipped. Returns the
        number of documents indexed.
        """
        if isinstance(store, (bytes, bytearray, str)):
            store = json.loads(store)
        if not isinstance(store, dict):
            return 0
        if "items" not in store:
            raise ValueError(
                "dependencies feed has no 'items' list; expected the "
                "Insality dependencies_store.json shape."
            )
        items = [it for it in store["items"] if isinstance(it, dict)]
        count = 0
        for item in items:
            asset_id = str(item.get("id", ""))
            if not asset_id:
                continue
            if item.get("unlisted") is True or asset_id in EXCLUDED_ASSET_IDS:
                continue
            self._upsert(
                f"assets:{asset_id}", "assets",
                str(item.get("title", asset_id)), "",
                str(item.get("description", "")),
                _asset_body(item),
                str(item.get("stars", 0)),
                _asset_url(item),
                tags=", ".join(str(t) for t in item.get("tags", [])),
            )
            count += 1
        return count

    def ingest_llms_tarball(self, tarball: bytes) -> dict[str, int]:
        """Stream a codeload tarball, indexing only ``*/llms/*`` members.

        Everything outside ``llms/`` (and non-Markdown members) is silently
        skipped. Returns per-scope counts of the documents indexed.
        """
        counts: dict[str, int] = {}
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            for member in tf.getmembers():
                match = _LLM_MEMBER_RE.match(member.name)
                if not match or not member.isfile():
                    continue
                if ".." in match["slug"].split("/"):
                    # A ``..`` path component would escape the llms/ tree.
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                text = extracted.read().decode("utf-8", errors="replace")
                slug = match["slug"]
                title = _title_from_markdown(text, slug)
                kind = match["kind"]
                if kind == "manuals":
                    self.ingest_manual(slug, title, text)
                    scope = "manuals"
                elif kind == "examples":
                    self.ingest_example(slug, title, text)
                    scope = "examples"
                else:
                    self.ingest_api_markdown(slug, title, text)
                    scope = self.api_scope_for(slug)
                counts[scope] = counts.get(scope, 0) + 1
        return counts

    @staticmethod
    def api_scope_for(slug: str) -> str:
        """Scope heuristic for ``llms/apis/<slug>.md``.

        A slug in :data:`KNOWN_EXTENSIONS` is an extension API; a ``dm``/``Dm``
        prefix is the C++ API; anything else is a Lua API.
        """
        if slug in KNOWN_EXTENSIONS:
            return "ext_api"
        if slug.startswith(("dm", "Dm")):
            return "cpp_api"
        return "lua_api"

    # -- search & retrieval ---------------------------------------------------

    def search(self, query: str, scope: str, limit: int = 10) -> list[dict[str, Any]]:
        """BM25-ranked FTS5 search within one scope.

        Exact-title matches are boosted one rank step above plain BM25;
        assets are ranked by ``stars DESC`` with BM25 as tiebreak. An unknown
        ``scope`` raises :class:`ValueError`.
        """
        if scope not in VALID_SCOPES:
            raise ValueError(
                f"unknown scope {scope!r}; expected one of {VALID_SCOPES}"
            )
        escaped = _escape_fts_query(query)
        if scope == "assets":
            rows = self._conn.execute(
                _ASSETS_SEARCH_SQL, (escaped, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                _SEARCH_SQL, (escaped, escaped, scope, limit)
            ).fetchall()
        return [
            {
                "doc_id": r[0],
                "scope": r[1],
                "title": r[2],
                "namespace": r[3],
                "summary": r[4],
                "stars": r[5],
                "url": r[6],
            }
            for r in rows
        ]

    def get_doc(self, doc_id: str, offset: int = 0,
                limit: int = DEFAULT_DOC_LIMIT) -> dict[str, Any]:
        """Return one document, paginated via ``offset``/``limit``.

        The default page is capped at 25,000 characters. An unknown
        ``doc_id`` raises :class:`KeyError`.
        """
        row = self._conn.execute(
            "SELECT title, scope, namespace, body FROM docs_fts WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise KeyError(doc_id)
        title, scope, namespace, body = row
        total = len(body)
        offset = max(0, offset)
        limit = max(1, limit)
        page = body[offset:offset + limit]
        return {
            "doc_id": doc_id,
            "title": title,
            "scope": scope,
            "namespace": namespace,
            "content": page,
            "total_len": total,
            "offset": offset,
            "truncated": offset + limit < total,
        }

    def stats(self) -> dict[str, int]:
        """Document counts per scope (scopes with zero docs are omitted)."""
        rows = self._conn.execute(
            "SELECT scope, count(*) FROM docs_fts GROUP BY scope"
        ).fetchall()
        return {scope: count for scope, count in rows}

    # -- fetch helpers (injectable client; never called with a real one in tests)

    def fetch_llms_tarball(self, *, client: httpx.Client | None = None) -> bytes:
        """Download the codeload ``llms/`` tarball."""
        return self._get_bytes(CODELOAD_TARBALL_URL, client=client)

    def fetch_asset_store(self, *, client: httpx.Client | None = None) -> dict:
        """Download and parse the Insality ``dependencies_store.json``."""
        data = self._get_bytes(config.ASSET_STORE_JSON_URL, client=client)
        return json.loads(data)

    def fetch_refdoc_zip(self, sha1: str, *,
                         client: httpx.Client | None = None) -> bytes:
        """Download the version-pinned ``ref-doc.zip`` for build ``sha1``."""
        url = f"{config.D_DEFOLD_BASE}/archive/{sha1}/engine/share/ref-doc.zip"
        return self._get_bytes(url, client=client)

    @staticmethod
    def _get_bytes(url: str, *, client: httpx.Client | None) -> bytes:
        owned = client is None
        # Follow redirects: every Defold distribution host redirects
        # (``d.defold.com/archive/...`` answers 301), and a bare client
        # would raise on the redirect response instead of fetching.
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
        http = (
            client
            if client is not None
            else httpx.Client(follow_redirects=True, timeout=timeout)
        )
        try:
            chunks: list[bytes] = []
            with http.stream("GET", url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
            return b"".join(chunks)
        finally:
            if owned:
                http.close()
