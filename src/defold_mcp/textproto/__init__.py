"""Defold Protobuf Text Format (textproto) parsing and decoding.

Public API:

- :func:`parse_textproto` — tolerant, line-preserving parse of Defold
  textproto into plain Python dicts/lists/scalars. Adjacent string literals
  concatenate; strings are unescaped exactly one level; 64-bit unsigned
  integers stay exact.
- :func:`decode_embedded_data` — post-process a parsed tree, recursively
  parsing ``data:`` proto strings on ``embedded_components`` /
  ``embedded_instances`` elements and attaching the result as ``data_ast``
  (tolerant: non-proto payloads are left untouched).
- :class:`BareIdent` — a ``str`` subclass marking bare (unquoted) identifier
  tokens (e.g. ``TYPE_BOX``) so the serializer can emit them unquoted.
- :class:`TextProtoError` — a ``ValueError`` subclass carrying 1-based line
  numbers for malformed input.
- :func:`serialize_textproto` — canonical, byte-exact serialisation of a
  parsed tree back to Defold textproto.
- :func:`escape_proto_multiline` — split a proto string into quoted
  multi-line fragments for the serializer.
- :func:`add_component` — append a component to a tree with embeddability
  guardrails.
- :func:`collision_from_image` — generate a box or convex-hull collision
  shape from a PNG.
- :func:`apply_operations` — apply a batch of mutation ops to a parsed tree.
"""

from defold_mcp.textproto.mutations import (
    add_component,
    apply_operations,
    collision_from_image,
)
from defold_mcp.textproto.parser import (
    BareIdent,
    TextProtoError,
    decode_embedded_data,
    parse_textproto,
)
from defold_mcp.textproto.serializer import (
    escape_proto_multiline,
    serialize_textproto,
)

__all__ = [
    "BareIdent",
    "TextProtoError",
    "add_component",
    "apply_operations",
    "collision_from_image",
    "decode_embedded_data",
    "escape_proto_multiline",
    "parse_textproto",
    "serialize_textproto",
]
