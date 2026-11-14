---
type: "Reference"
title: "Textproto engine"
openwiki_generated: true
verified:
  - by: openwiki/0.9.0
    at: 2026-11-14T22:05:56.160Z
sources:
  - id: openwiki-source-467bf05f14c0b05e12ede3ab
    resource: repo://src/defold_mcp/textproto/parser.py
  - id: openwiki-source-920c288fa775427e5500aec9
    resource: repo://src/defold_mcp/textproto/schemas.py
  - id: openwiki-source-edd5cc407110cd57fc145906
    resource: repo://src/defold_mcp/textproto/serializer.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-14T22:05:56.160Z" }
---


# Textproto engine

`src/defold_mcp/textproto/` exists because hand-editing Defold's Protobuf Text Format files is the single most common way LLM agents corrupt Defold projects: the nested `data:` strings use escaping so fiddly that even a careful model gets it wrong. The engine's contract removes that class of failure entirely:

> **Round-trip contract:** `serialize_textproto(parse_textproto(fixture)) == fixture` holds **byte-exact** for the verified Defold 1.14.1 fixtures embedded in the test suite (a full `.go`, the double-escaped `.collection`, and a scalar suite), and `parse_textproto(serialize_textproto(tree)) == tree` for mutated trees. The tests pin the exact fixture bytes, including the infamous inner closing-quote line.

## The two escape levels

Defold stores nested protos as strings inside strings:

- **Level 1 (`.go` file):** `embedded_components.data` carries a component proto encoded in adjacent-chunk layout — one double-quoted chunk per source line, C-style escapes (`\"`, `\n`), terminated by the empty-literal `""` sentinel. The parser concatenates adjacent literals and unescapes once; the serializer re-splits per line.
- **Level 2 (`.collection` file):** `embedded_instances.data` carries a whole inline game object. Its value (after one unescape) is a `.go`-style text in merged form — the inner component's data span is one continuous string where the inner proto's newlines are two-character `\n` sequences. The file emits one chunk per line, with chunk boundaries inside the data span falling at each inner escape position, structural lines owning their chunks, and the inner string's closing quote on its own chunk at the outer escape level: `"\"\n"` — never the double-escaped `"\"\\n"`. A dedicated test pins exactly that line.

`escape_proto_multiline(text, level=1)` is the public chunking helper. Its `level` parameter is currently a no-op and documented as reserved — the serializer derives nesting from the tree itself, and the byte-exact fixtures prove the current emission correct.

## The parser

`parse_textproto(text, schema=<always-list field names>)` — a tolerant recursive-descent parser over a hand-written tokeniser:

- Scalars: floats (including scientific notation), decimal and hex ints, booleans, quoted strings with C escapes (`\n`, `\t`, `\r`, `\"`, `\\`, `\xNN` hex, `\NNN` octal, UTF-8 content), adjacent-literal concatenation, and bare enum-like tokens.
- `BareIdent(str)`: bare tokens (not `true`/`false`, not numeric) are wrapped in this `str` subclass so quotedness is carried by the **type**, not inferred from content. `id: "HUD"` stays a plain string; `TYPE_BOX` stays a bare enum — and they round-trip that way.
- Blocks: `key { … }` and the tolerated `key: { … }` form; repeated fields become lists; the `schema` set forces named fields to lists even when single, so downstream mutation shapes stay predictable.
- 64-bit unsigned ints (Defold hashes) parse as exact Python ints — never through float.
- Errors raise `TextProtoError` (a `ValueError`) with 1-based line numbers; `max_depth` guards runaway nesting.
- `decode_embedded_data(tree, max_depth=3)` recursively parses `data` payloads under `embedded_components`/`embedded_instances` into `data_ast` (deep-copied, `BareIdent`-preserving), leaving non-proto payloads untouched and untainted.

## The serializer

`serialize_textproto(tree)` inverts the parse in canonical form:

- **Quoting guarantee:** `BareIdent` values emit unquoted **only** when they fullmatch the ASCII ident pattern `[A-Za-z_][A-Za-z0-9_]*` (`_IDENT_RE`); every other `str` value — no matter how enum-like it looks — emits quoted and escaped. The charset guard means a programmatically-constructed `BareIdent('bad"value')` degrades safely to quoted output rather than emitting raw invalid textproto.
- **Control characters:** scalar strings escape `\t`, `\r`, `\n` by name and other chars < 0x20 as `\xNN` (`_escape_scalar`); the data-chunk path keeps its own newline-level escaping (`_escape_for_embed`).
- **Math default omission:** `position`/`size` omit zero components (an all-zero full block disappears; a partial 2D block is kept because the absent `z` is meaningful), `scale` all-1.0 and identity `rotation` blocks are omitted entirely, and only non-default components emit. `MATH_DEFAULTS["size"]` is provisional pending real `.gui` fixture verification.
- **Floats:** `repr()`-based, always at least one decimal (`200.0`); the `.0` guarantee holds within [1e-4, 1e16) — outside that, scientific notation appears (value-stable, documented).
- `data_ast` is preferred over the raw `data` string when both are present, so mutations mutate the AST and the serializer re-encodes from structure.

## Schemas (the documented deferral)

`schemas.py` holds the canonical field-order tables (`CollectionDesc`: name, instances, collection_instances, scale_along_z, embedded_instances; `PrototypeDesc`: components, embedded_components; best-effort orders for atlas/tilesource/tilegrid/gui/collisionobject/material), `ALWAYS_LIST_FIELDS`, `NON_EMBEDDABLE_TYPES`, `EMBED_TYPE_NORMALISATION` and `MATH_DEFAULTS`. **The order tables are not yet wired into the serializer** — which follows dict insertion order (file order for parsed trees) — and carry a prominent comment that the server-wiring task MUST wire them before any freshly-created resource is serialised. Round-trip correctness never depended on them; freshly-built trees do.

See [mutations and collision shapes](mutations-and-collision-shapes.md) for the mutation layer above this engine.
