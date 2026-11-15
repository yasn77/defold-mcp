"""Canonical Defold Protobuf Text serializer.

Turns a parsed (and optionally ``decode_embedded_data``-decoded) tree back
into byte-exact Defold Editor textproto: canonical field order, math-default
omission, floats that always emit ``.0``, and single- / double-escaped
``data:`` strings built from adjacent string literals.

The parser (:mod:`defold_mcp.textproto.parser`) is the byte-level oracle: the
acceptance criterion is that ``serialize(parse(fixture)) == fixture``.
"""

from __future__ import annotations

import re

from defold_mcp.textproto import schemas
from defold_mcp.textproto.parser import BareIdent

__all__ = ["escape_proto_multiline", "serialize_textproto"]

# ASCII identifier pattern: the safe charset for unquoted textproto tokens.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Fields whose elements carry an embedded proto in a ``data`` string.
_EMBEDDED_FIELDS = ("embedded_components", "embedded_instances")

# A ``data`` field is re-serialised from its ``data_ast`` (the source of truth
# after mutation) only when the owning element is one of these.
_DATA_CARRIERS = _EMBEDDED_FIELDS


# ---------------------------------------------------------------------------
# Escape helpers
# ---------------------------------------------------------------------------


def _escape_level1(text: str) -> str:
    """Escape one level of proto text: ``\\`` -> ``\\\\``, ``"`` -> ``\\"``.

    Real newlines are left untouched (they are handled by the chunker).
    """
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return "".join(out)


def _escape_scalar(text: str) -> str:
    """Escape a scalar string value for quoted textproto emission.

    Escapes backslash, double-quote, and all control characters below 0x20:
    ``\\t``, ``\\r``, ``\\n`` by name; all others as ``\\xNN``.
    """
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\n":
            out.append("\\n")
        elif cp < 0x20:
            out.append(f"\\x{cp:02x}")
        else:
            out.append(ch)
    return "".join(out)


def escape_proto_multiline(text: str, level: int = 1) -> list[str]:
    """Chunk proto text into adjacent string literals for a ``data:`` field.

    Returns the list of literal fragments (without surrounding quotes) that,
    joined as ``"f0" "f1" ... "fn" ""``, reproduce ``text`` exactly one level
    deeper.

    ``level`` is reserved and currently a no-op: the serializer derives
    nesting from the tree itself, so callers should pass the default.

    Chunk boundaries fall at every real newline AND at every two-character
    ``\\n`` escape (the newlines inside a nested ``data`` string), with the
    ``\\n`` kept on the preceding fragment.  A two-character ``\\n`` that
    immediately follows a closing brace (``}``) is NOT a boundary: the brace
    and its trailing escape form a single unit (the end of a nested block).
    Each fragment is escaped one level.
    """
    fragments: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            fragments.append(_escape_level1("".join(buf)))
            buf.clear()

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            # Real newline: a structural chunk boundary.
            buf.append(ch)
            i += 1
            flush()
        elif ch == "\\" and i + 1 < n and text[i + 1] == "n":
            # Two-char \n inside a nested data string. A chunk boundary
            # unless the preceding character is a closing brace (}).
            prev = buf[-1] if buf else ""
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            if prev != "}":
                flush()
        else:
            buf.append(ch)
            i += 1
    flush()
    return fragments


# ---------------------------------------------------------------------------
# Scalar / string formatting
# ---------------------------------------------------------------------------


def _format_scalar(value: object) -> str:
    """Render a scalar for textproto output.

    ``BareIdent`` values (bare enum-like tokens parsed unquoted) are emitted
    unquoted.  All other strings are emitted quoted with control characters
    escaped (``\\t``, ``\\r``, ``\\xNN`` for chars < 0x20).

    Floats use ``repr()``: the ``.0`` suffix is guaranteed within
    [1e-4, 1e16); outside that range scientific notation may appear
    (value-stable, round-trip safe).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, BareIdent) and _IDENT_RE.fullmatch(value):
        return str(value)
    if isinstance(value, str):
        return f'"{_escape_scalar(value)}"'
    raise TypeError(f"Cannot format scalar: {type(value).__name__}")


def _escape_for_embed(text: str) -> str:
    """Escape text for a single-quoted proto string (one level)."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Math-default omission
# ---------------------------------------------------------------------------


def _math_block_lines(
    key: str, value: dict, indent: int
) -> tuple[bool, list[str]]:
    """Apply math-default omission. Returns (omitted, lines)."""
    defaults = schemas.MATH_DEFAULTS[key]
    prefix = "  " * indent
    is_full = set(value.keys()) == set(defaults.keys())
    if is_full:
        if all(value[k] == defaults[k] for k in defaults):
            return True, []
        lines = [
            f"{prefix}{k}: {_format_scalar(v)}"
            for k, v in value.items()
            if v != defaults[k]
        ]
        return False, lines
    # Partial block: keep verbatim (absence of a component is meaningful).
    lines = [f"{prefix}{k}: {_format_scalar(v)}" for k, v in value.items()]
    return False, lines


# ---------------------------------------------------------------------------
# Clean-text serialisation (for embedding inside a data: string)
# ---------------------------------------------------------------------------


def _clean_lines(node: dict, indent: int = 0) -> list[str]:
    """Serialise *node* to clean proto-text lines.

    ``data:`` fields become single-quoted strings with ``\\n`` / ``\\"``
    escapes (one level), suitable for embedding inside an outer ``data:``
    payload.  The ``data_ast`` metadata key is never emitted.
    """
    lines: list[str] = []
    prefix = "  " * indent
    for key, value in node.items():
        if key == "data_ast":
            continue
        if isinstance(value, dict):
            if key in schemas.MATH_DEFAULTS:
                omitted, sub = _math_block_lines(key, value, indent + 1)
                if omitted:
                    continue
                lines.append(f"{prefix}{key} {{")
                lines.extend(sub)
                lines.append(f"{prefix}}}")
            else:
                lines.append(f"{prefix}{key} {{")
                lines.extend(_clean_lines(value, indent + 1))
                lines.append(f"{prefix}}}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{prefix}{key} {{")
                    lines.extend(_clean_lines(item, indent + 1))
                    lines.append(f"{prefix}}}")
                else:
                    lines.append(f"{prefix}{key}: {_format_scalar(item)}")
        elif key == "data" and isinstance(value, str):
            inner = node.get("data_ast")
            clean = (
                "\n".join(_clean_lines(inner)) + "\n"
                if inner is not None
                else value
            )
            lines.append(f'{prefix}{key}: "{_escape_for_embed(clean)}"')
        else:
            lines.append(f"{prefix}{key}: {_format_scalar(value)}")
    return lines


# ---------------------------------------------------------------------------
# Top-level serialisation (data: as adjacent string literals)
# ---------------------------------------------------------------------------


def _top_lines(node: dict, indent: int, lines: list[str]) -> None:
    """Append serialised lines for *node* to *lines*."""
    prefix = "  " * indent
    for key, value in node.items():
        if key == "data_ast":
            continue
        if isinstance(value, dict):
            if key in schemas.MATH_DEFAULTS:
                omitted, sub = _math_block_lines(key, value, indent + 1)
                if omitted:
                    continue
                lines.append(f"{prefix}{key} {{")
                lines.extend(sub)
                lines.append(f"{prefix}}}")
            else:
                lines.append(f"{prefix}{key} {{")
                _top_lines(value, indent + 1, lines)
                lines.append(f"{prefix}}}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{prefix}{key} {{")
                    _top_lines(item, indent + 1, lines)
                    lines.append(f"{prefix}}}")
                else:
                    lines.append(f"{prefix}{key}: {_format_scalar(item)}")
        elif key == "data" and isinstance(value, str):
            inner = node.get("data_ast")
            if inner is not None:
                clean = "\n".join(_clean_lines(inner)) + "\n"
                frags = escape_proto_multiline(clean)
                two_char_nl = "\\" + "n"
                first = frags[0].replace("\n", two_char_nl) if frags else ""
                lines.append(f'{prefix}{key}: "{first}"')
                for frag in frags[1:]:
                    lines.append(f'{prefix}"{frag.replace(chr(10), two_char_nl)}"')
                lines.append(f'{prefix}""')
            else:
                lines.append(f'{prefix}{key}: "{_escape_for_embed(value)}"')
        else:
            lines.append(f"{prefix}{key}: {_format_scalar(value)}")


def _ordered_keys(node: dict, extension: str | None) -> list[str]:
    """Top-level keys in canonical order for ``extension``.

    Keys listed in :data:`~defold_mcp.textproto.schemas.SCHEMA_BY_EXTENSION`
    are emitted in the Defold Editor's own write order; any remaining key
    keeps its existing position after them (``sorted`` is stable). Without
    an extension the tree's own order is preserved, which is what the
    byte-exact round-trip contract relies on.
    """
    keys = list(node)
    order = schemas.SCHEMA_BY_EXTENSION.get(extension) if extension else None
    if order is None:
        return keys
    rank = {name: index for index, name in enumerate(order)}
    return sorted(keys, key=lambda key: rank.get(key, len(rank)))


def serialize_textproto(tree: dict, *, extension: str | None = None) -> str:
    """Serialise a parsed (and optionally decoded) tree to Defold textproto.

    Field order follows the dict insertion order (i.e. the parse / file
    order) unless ``extension`` is given, in which case the top-level keys
    are re-ordered into the canonical Defold write order for that resource
    type.  Math-default omission, float ``.0`` formatting, and
    ``data:``-as-adjacent-literals are applied.  The result ends with a
    trailing newline when non-empty.
    """
    ordered = {key: tree[key] for key in _ordered_keys(tree, extension)}
    lines: list[str] = []
    _top_lines(ordered, 0, lines)
    return "\n".join(lines) + "\n" if lines else ""
