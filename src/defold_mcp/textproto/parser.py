"""Tolerant, lossless-enough parser for Defold Protobuf Text Format files.

Defold resource files (``.collection``, ``.go``, ``.gui``, ...) are written
in the Protobuf Text Format: ``field: value`` scalars, ``field { ... }``
message blocks, ``#`` comments and adjacent string-literal concatenation.

The parser decodes strings exactly one level (``\"`` becomes ``"``, ``\\n``
becomes a real newline), concatenates adjacent string literals, and keeps
64-bit unsigned integers exact (Python ints are arbitrary precision, so no
coercion through float ever happens).

Malformed input always raises :class:`TextProtoError` with a 1-based line
number; nothing is silently truncated.
"""

from __future__ import annotations

import copy
import re

__all__ = ["BareIdent", "TextProtoError", "decode_embedded_data", "parse_textproto"]

# Field names whose elements carry an embedded proto in a ``data`` string.
_EMBEDDED_FIELDS = ("embedded_components", "embedded_instances")

# A textproto number: optional sign, then hex or decimal, with an optional
# fractional part and/or exponent. Classification into int vs float happens
# afterwards based on which characters are present.
_NUMBER_RE = re.compile(
    r"[+-]?(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)"
)

_HEX_DIGITS = set("0123456789abcdefABCDEF")
_OCT_DIGITS = set("01234567")


class BareIdent(str):
    """A bare (unquoted) identifier token from textproto, e.g. ``TYPE_BOX``.

    Subclasses :class:`str` so equality, hashing and ``repr`` behave
    identically to a plain string.  The serializer uses the type to decide
    whether to emit the token unquoted (``BareIdent``) or quoted (plain
    ``str``), preserving the byte-exact inverse contract.
    """


class TextProtoError(ValueError):
    """Raised when Defold textproto input is malformed.

    The message always carries a 1-based line number so callers can point
    the user at the offending line.
    """


def parse_textproto(
    text: str,
    *,
    max_depth: int = 32,
    schema: frozenset[str] | set[str] | None = None,
) -> dict:
    """Parse Defold textproto into a dict of Python values.

    Field order is preserved. A field that appears more than once becomes a
    list in file order; a field appearing once is a scalar (or dict for a
    message block). Pass ``schema`` with field names that must ALWAYS be
    lists, even when they occur exactly once, so downstream mutation is
    predictable.

    Raises:
        TextProtoError: on unterminated strings, missing colons, trailing
            junk, or nesting deeper than ``max_depth``.
    """
    tokens = _tokenize(text)
    tree, pos = _parse_fields(tokens, 0, 0, max_depth, top=True)
    if pos != len(tokens):
        kind, value, line, _col = tokens[pos]
        raise TextProtoError(f"line {line}: unexpected {value!r} after end of message")
    if schema:
        _apply_schema(tree, set(schema))
    return tree


def decode_embedded_data(tree: dict, *, max_depth: int = 3) -> dict:
    """Recursively decode ``data:`` proto strings into ``data_ast`` keys.

    For every element of ``embedded_components`` and ``embedded_instances``
    (single dict or list of dicts) whose ``data`` field is a string, attempt
    to parse it as textproto. On success the parsed tree is attached as
    ``data_ast`` alongside the original ``data`` string, and decoding
    recurses into it up to ``max_depth`` levels (an ``embedded_instances``
    ``data_ast`` contains ``embedded_components`` whose data is itself a
    proto). On parse failure the ``data`` string is left untouched with no
    ``data_ast`` key, so non-proto payloads (raw shader snippets, etc.)
    survive.

    The input tree is not mutated; a new structure is returned.
    """
    result = copy.deepcopy(tree)
    _decode(result, 1, max_depth)
    return result


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[tuple[str, str, int, int]]:
    """Split textproto text into (kind, value, line, col) tokens.

    Kinds: ``ident``, ``number`` (raw text), ``string`` (decoded value),
    ``colon``, ``lbrace``, ``rbrace``. Comments and whitespace are dropped;
    1-based line numbers are tracked through strings and comments.
    """
    tokens: list[tuple[str, str, int, int]] = []
    i = 0
    n = len(text)
    line = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch in " \t\r":
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "{":
            tokens.append(("lbrace", "{", line, i))
            i += 1
        elif ch == "}":
            tokens.append(("rbrace", "}", line, i))
            i += 1
        elif ch == ":":
            tokens.append(("colon", ":", line, i))
            i += 1
        elif ch == '"':
            start_line = line
            i, value = _read_string(text, i, line)
            tokens.append(("string", value, start_line, i))
            line = _line_at(text, i)
        elif ch.isdigit() or (ch in "+-" and _looks_like_number_start(text, i)):
            match = _NUMBER_RE.match(text, i)
            if match is None:
                raise TextProtoError(f"line {line}: malformed number near {text[i:i + 8]!r}")
            raw = match.group(0)
            tokens.append(("number", raw, line, i))
            i = match.end()
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("ident", text[i:j], line, i))
            i = j
        else:
            raise TextProtoError(f"line {line}: unexpected character {ch!r}")
    return tokens


def _looks_like_number_start(text: str, i: int) -> bool:
    """True if text[i:] can start a number (guards against stray signs)."""
    if i + 1 >= len(text):
        return False
    nxt = text[i + 1]
    return nxt.isdigit() or (nxt == "." and i + 2 < len(text) and text[i + 2].isdigit())


def _line_at(text: str, pos: int) -> int:
    """1-based line number at the given position (after a token)."""
    return 1 + text.count("\n", 0, pos)


def _read_string(text: str, i: int, line: int) -> tuple[int, str]:
    """Read one double-quoted string starting at text[i] == '"'.

    Returns (position-after-closing-quote, decoded value). C-style escapes
    are decoded exactly one level: ``\\n``, ``\\t``, ``\\r``, ``\\"``,
    ``\\\\``, ``\\'``, ``\\xNN`` and ``\\NNN`` (octal byte). Raw newlines
    inside the string are allowed (they occur in nested ``data:`` payloads
    after one level of unescaping).
    """
    n = len(text)
    start_line = line
    i += 1  # skip opening quote
    buf: list[str] = []
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 >= n:
                raise TextProtoError(f"line {line}: unterminated escape in string")
            esc = text[i + 1]
            if esc == "n":
                buf.append("\n")
                i += 2
            elif esc == "t":
                buf.append("\t")
                i += 2
            elif esc == "r":
                buf.append("\r")
                i += 2
            elif esc in ('"', "'"):
                buf.append(esc)
                i += 2
            elif esc == "\\":
                buf.append("\\")
                i += 2
            elif esc == "x":
                digits = text[i + 2 : i + 4]
                if len(digits) != 2 or not set(digits) <= _HEX_DIGITS:
                    raise TextProtoError(f"line {line}: invalid hex escape \\x{digits}")
                buf.append(chr(int(digits, 16)))
                i += 4
            elif esc in _OCT_DIGITS:
                j = i + 1
                oct_digits = ""
                while j < n and len(oct_digits) < 3 and text[j] in _OCT_DIGITS:
                    oct_digits += text[j]
                    j += 1
                buf.append(chr(int(oct_digits, 8)))
                i = j
            else:
                raise TextProtoError(f"line {line}: unknown escape '\\{esc}' in string")
        elif c == '"':
            return i + 1, "".join(buf)
        else:
            if c == "\n":
                line += 1
            buf.append(c)
            i += 1
    raise TextProtoError(f"line {start_line}: unterminated string")


# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------


def _parse_fields(
    tokens: list[tuple[str, str, int, int]],
    pos: int,
    depth: int,
    max_depth: int,
    *,
    top: bool,
) -> tuple[dict, int]:
    """Parse fields until the matching '}' (or EOF when ``top``)."""
    result: dict = {}
    n = len(tokens)
    while pos < n:
        kind, value, line, _col = tokens[pos]
        if kind == "rbrace":
            if top:
                raise TextProtoError(f"line {line}: unexpected '}}'")
            return result, pos + 1
        if kind != "ident":
            raise TextProtoError(f"line {line}: expected a field name, got {value!r}")
        key = value
        pos += 1
        if pos >= n:
            raise TextProtoError(f"line {line}: field '{key}' has no value")
        nkind, nvalue, nline, _ncol = tokens[pos]
        if nkind == "colon":
            pos += 1
            if pos >= n:
                raise TextProtoError(f"line {nline}: field '{key}' has no value")
            vkind, vvalue, vline, _vcol = tokens[pos]
            if vkind == "lbrace":
                pos += 1
                sub, pos = _open_block(tokens, pos, vline, depth, max_depth)
                _assign(result, key, sub)
            elif vkind == "string":
                # Adjacent string literals concatenate into one logical
                # string (the standard textproto rule for multi-line data).
                parts: list[str] = []
                while pos < n and tokens[pos][0] == "string":
                    parts.append(tokens[pos][1])
                    pos += 1
                _assign(result, key, "".join(parts))
            elif vkind == "number":
                _assign(result, key, _convert_number(vvalue, vline))
                pos += 1
            elif vkind == "ident":
                if vvalue == "true":
                    _assign(result, key, True)
                elif vvalue == "false":
                    _assign(result, key, False)
                else:
                    # Bare enum-like token: wrap in BareIdent so the
                    # serializer can emit it unquoted.
                    _assign(result, key, BareIdent(vvalue))
                pos += 1
            else:
                raise TextProtoError(
                    f"line {vline}: expected a value for field '{key}', got {vvalue!r}"
                )
        elif nkind == "lbrace":
            pos += 1
            sub, pos = _open_block(tokens, pos, nline, depth, max_depth)
            _assign(result, key, sub)
        else:
            raise TextProtoError(
                f"line {nline}: expected ':' or '{{' after field '{key}', got {nvalue!r}"
            )
    if top:
        return result, pos
    raise TextProtoError("unexpected end of input: missing '}'")


def _open_block(
    tokens: list[tuple[str, str, int, int]],
    pos: int,
    line: int,
    depth: int,
    max_depth: int,
) -> tuple[dict, int]:
    """Enter a message block, enforcing the nesting limit."""
    if depth + 1 > max_depth:
        raise TextProtoError(f"line {line}: maximum nesting depth {max_depth} exceeded")
    return _parse_fields(tokens, pos, depth + 1, max_depth, top=False)


def _assign(target: dict, key: str, value: object) -> None:
    """Insert a field value, promoting to a list on repetition (file order)."""
    if key not in target:
        target[key] = value
    elif isinstance(target[key], list):
        target[key].append(value)
    else:
        target[key] = [target[key], value]


def _convert_number(raw: str, line: int) -> int | float:
    """Convert a raw number token; 64-bit unsigned values stay exact ints."""
    if "x" in raw or "X" in raw:
        return int(raw, 16)
    if any(c in raw for c in ".eE"):
        return float(raw)
    return int(raw)


def _apply_schema(node: object, names: set[str]) -> None:
    """Force schema-hinted field names to always be lists, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in names and not isinstance(value, list):
                node[key] = [value]
            _apply_schema(node[key], names)
    elif isinstance(node, list):
        for item in node:
            _apply_schema(item, names)


# ---------------------------------------------------------------------------
# Recursive data: decoder
# ---------------------------------------------------------------------------


def _decode(node: object, depth: int, max_depth: int) -> None:
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in _EMBEDDED_FIELDS:
            elements = value if isinstance(value, list) else [value]
            for element in elements:
                if isinstance(element, dict) and isinstance(element.get("data"), str):
                    try:
                        # Force the embedded fields to lists so downstream
                        # mutation is predictable at every nesting level.
                        ast = parse_textproto(
                            element["data"], schema=frozenset(_EMBEDDED_FIELDS)
                        )
                    except TextProtoError:
                        # Tolerant: non-proto payloads survive untouched.
                        continue
                    element["data_ast"] = ast
                    if depth < max_depth:
                        _decode(ast, depth + 1, max_depth)
        elif isinstance(value, dict):
            _decode(value, depth, max_depth)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _decode(item, depth, max_depth)
