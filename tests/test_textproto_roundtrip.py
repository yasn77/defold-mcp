"""Tests for the Defold textproto parser, serializer, and mutators.

Part A: parser + decode_embedded_data.
Part B: serializer round-trip, math-default omission, guardrails,
        and collision_from_image.
"""

import io

import pytest
from PIL import Image

from defold_mcp.textproto import (
    BareIdent,
    TextProtoError,
    decode_embedded_data,
    parse_textproto,
)
from defold_mcp.textproto.serializer import serialize_textproto
from defold_mcp.textproto.mutations import (
    add_component,
    collision_from_image,
)

# Verified Defold 1.14.1 output: a .go file with a single-escaped
# embedded_components data string built from adjacent literals.
GO_FIXTURE = r'''embedded_components {
  id: "sprite"
  type: "sprite"
  data: "default_animation: \"idle\"\n"
  "material: \"/builtins/materials/sprite.material\"\n"
  "textures {\n"
  "  sampler: \"texture_sampler\"\n"
  "  texture: \"/main/main.atlas\"\n"
  "}\n"
  ""
}
'''

# The one-level-unescaped content of the data string above.
EXPECTED_GO_DATA = (
    'default_animation: "idle"\n'
    'material: "/builtins/materials/sprite.material"\n'
    "textures {\n"
    '  sampler: "texture_sampler"\n'
    '  texture: "/main/main.atlas"\n'
    "}\n"
)

# Verified Defold 1.14.1 output: a .collection embedded_instances block
# whose data string is a .go-style proto (double-escaped overall).
COLLECTION_FIXTURE = r'''embedded_instances {
  id: "camera"
  data: "embedded_components {\n"
  "  id: \"camera\"\n"
  "  type: \"camera\"\n"
  "  data: \"aspect_ratio: 1.0\\n"
  "fov: 0.7854\\n"
  "near_z: -1.0\\n"
  "far_z: 1.0\\n"
  "orthographic_projection: 1\\n"
  "\"\n"
  "}\n"
  ""
  position {
    x: 640.0
    y: 360.0
  }
}
'''

# The collection data string after ONE level of unescaping: the inner
# data: string is still one level escaped (\\n stays a literal backslash-n).
EXPECTED_COLLECTION_DATA = (
    "embedded_components {\n"
    '  id: "camera"\n'
    '  type: "camera"\n'
    '  data: "aspect_ratio: 1.0\\n'
    'fov: 0.7854\\n'
    'near_z: -1.0\\n'
    'far_z: 1.0\\n'
    'orthographic_projection: 1\\n'
    '"\n'
    "}\n"
)

# The inner camera component proto, fully clean after two levels.
EXPECTED_INNER_PROTO = (
    "aspect_ratio: 1.0\n"
    "fov: 0.7854\n"
    "near_z: -1.0\n"
    "far_z: 1.0\n"
    "orthographic_projection: 1\n"
)

# Verified Defold 1.14.1 output: a full .go file with a referenced
# ``components`` entry followed by an ``embedded_components`` entry whose
# data string is built from Level-1 adjacent string literals.
GO_FULL_FIXTURE = r'''components {
  id: "script"
  component: "/main/hero.script"
}
embedded_components {
  id: "sprite"
  type: "sprite"
  data: "default_animation: \"idle\"\n"
  "material: \"/builtins/materials/sprite.material\"\n"
  "textures {\n"
  "  sampler: \"texture_sampler\"\n"
  "  texture: \"/main/main.atlas\"\n"
  "}\n"
  ""
}
'''


def test_parse_scalar_forms():
    text = (
        "a: 200.0\n"
        "b: 0.7854\n"
        "c: 1e-3\n"
        "d: 10\n"
        "e: 0x1f\n"
        "f: true\n"
        "g: false\n"
        "h: TYPE_BOX\n"
        "i: -1.0\n"
    )
    tree = parse_textproto(text)
    assert tree["a"] == 200.0
    assert isinstance(tree["a"], float)
    assert tree["b"] == 0.7854
    assert tree["c"] == 0.001
    assert isinstance(tree["c"], float)
    assert tree["d"] == 10
    assert isinstance(tree["d"], int)
    assert tree["e"] == 31
    assert isinstance(tree["e"], int)
    assert tree["f"] is True
    assert tree["g"] is False
    assert tree["h"] == "TYPE_BOX"
    assert tree["i"] == -1.0
    assert isinstance(tree["i"], float)


def test_adjacent_string_concatenation():
    tree = parse_textproto(GO_FIXTURE)
    comp = tree["embedded_components"]
    assert comp["id"] == "sprite"
    assert comp["type"] == "sprite"
    # Adjacent literals concatenate into one logical string, unescaped
    # exactly one level (real newlines, real quotes).
    assert comp["data"] == EXPECTED_GO_DATA


def test_double_escaped_collection():
    parsed = parse_textproto(
        COLLECTION_FIXTURE,
        schema={"embedded_instances", "embedded_components"},
    )
    inst = parsed["embedded_instances"][0]
    assert inst["id"] == "camera"
    # One level unescaped: the inner data string is still escaped.
    assert inst["data"] == EXPECTED_COLLECTION_DATA
    assert inst["position"] == {"x": 640.0, "y": 360.0}

    decoded = decode_embedded_data(parsed)
    dec_inst = decoded["embedded_instances"][0]
    assert "data_ast" in dec_inst
    comp = dec_inst["data_ast"]["embedded_components"][0]
    # Fully clean proto text after the second level.
    assert comp["data"] == EXPECTED_INNER_PROTO
    inner = comp["data_ast"]
    assert inner["aspect_ratio"] == 1.0
    assert inner["fov"] == 0.7854
    assert inner["near_z"] == -1.0
    assert inner["far_z"] == 1.0
    assert inner["orthographic_projection"] == 1


def test_repeated_blocks_list_normalisation():
    two = parse_textproto('instances { id: "a" } instances { id: "b" }')
    assert two["instances"] == [{"id": "a"}, {"id": "b"}]

    single_hinted = parse_textproto('instances { id: "a" }', schema={"instances"})
    assert single_hinted["instances"] == [{"id": "a"}]

    single_plain = parse_textproto('instances { id: "a" }')
    assert single_plain["instances"] == {"id": "a"}


def test_large_uint64_preserved():
    tree = parse_textproto("id: 11844936738040519888")
    assert isinstance(tree["id"], int)
    assert tree["id"] == 11844936738040519888


def test_decode_tolerates_non_proto_data():
    text = (
        "embedded_components {\n"
        '  id: "material"\n'
        '  type: "material"\n'
        '  data: "float4(1, 2, 3, 4)"\n'
        "}\n"
    )
    decoded = decode_embedded_data(parse_textproto(text))
    comp = decoded["embedded_components"]
    assert comp["data"] == "float4(1, 2, 3, 4)"
    assert "data_ast" not in comp


def test_parse_errors_carry_line_numbers():
    with pytest.raises(TextProtoError) as exc_unterminated:
        parse_textproto('title: "ok"\nbroken: "abc\n')
    assert "line 2" in str(exc_unterminated.value)

    with pytest.raises(TextProtoError) as exc_colon:
        parse_textproto("title: fine\nbroken value\n")
    assert "line 2" in str(exc_colon.value)


def test_comments_and_blank_lines():
    text = (
        "# leading comment\n"
        "\n"
        "a: 1\n"
        "   # indented comment\n"
        "\n"
        "b { c: 2 }\n"
        "# trailing comment\n"
    )
    tree = parse_textproto(text)
    assert tree == {"a": 1, "b": {"c": 2}}


def test_colon_brace_form_matches_brace_form():
    """The tolerated ``key: { ... }`` colon-brace form parses identically
    to the canonical ``key { ... }`` brace form."""
    brace = parse_textproto("key { inner: 1 }\n")
    colon_brace = parse_textproto("key: { inner: 1 }\n")
    assert colon_brace == brace
    assert colon_brace == {"key": {"inner": 1}}


def test_max_depth_overflow_raises():
    text = "a { b { c { d: 1 } } }\n"
    with pytest.raises(TextProtoError):
        parse_textproto(text, max_depth=2)


def test_hex_and_octal_escape_decoding():
    tree = parse_textproto(r's: "\x41"' + "\n" + r't: "\101"' + "\n")
    assert tree["s"] == "A"
    assert tree["t"] == "A"


# ---------------------------------------------------------------------------
# Part B: serializer round-trip, math-default omission, guardrails,
#         and collision_from_image
# ---------------------------------------------------------------------------

# Canonical .collection fixture with instances, collection_instances,
# scale_along_z, and double-nested embedded_instances (camera + sprite).
# Written as a raw string so \n, \", \\ are literal in the file.
FULL_COLLECTION_FIXTURE = r'''name: "main"
instances {
  id: "/hero"
  position {
    x: 200.0
  }
}
collection_instances {
  id: "sub"
  collection: "/sub/level.collection"
  position {
    x: 0.0
    y: 0.0
  }
}
scale_along_z: 0
embedded_instances {
  id: "camera"
  data: "embedded_components {\n"
  "  id: \"camera\"\n"
  "  type: \"camera\"\n"
  "  data: \"aspect_ratio: 1.0\\n"
  "fov: 0.7854\\n"
  "near_z: -1.0\\n"
  "far_z: 1.0\\n"
  "orthographic_projection: 1\\n"
  "\"\n"
  "}\n"
  "embedded_components {\n"
  "  id: \"sprite\"\n"
  "  type: \"sprite\"\n"
  "  data: \"default_animation: \\\"idle\\\"\\n"
  "material: \\\"/builtins/materials/sprite.material\\\"\\n"
  "textures {\\n"
  "  sampler: \\\"texture_sampler\\\"\\n"
  "  texture: \\\"/main/main.atlas\\\"\\n"
  "}\\n\"\n"
  "}\n"
  ""
  position {
    x: 640.0
    y: 360.0
  }
}
'''


class TestSerializerRoundTrip:
    """parse → decode → serialize must be byte-exact."""

    def test_full_collection_roundtrip(self):
        tree = parse_textproto(
            FULL_COLLECTION_FIXTURE,
            schema={"instances", "collection_instances", "embedded_instances"},
        )
        decoded = decode_embedded_data(tree)
        result = serialize_textproto(decoded)
        assert result == FULL_COLLECTION_FIXTURE

    def test_inner_closing_quote_single_backslash_n(self):
        """The inner camera data closing-quote line must be  "\\\"\\n"
        (escaped quote then single \\n), NOT double-escaped."""
        tree = parse_textproto(
            FULL_COLLECTION_FIXTURE,
            schema={"instances", "collection_instances", "embedded_instances"},
        )
        decoded = decode_embedded_data(tree)
        result = serialize_textproto(decoded)
        lines = result.split("\n")
        # Find the line that is the inner closing quote of the camera data.
        # It should be exactly:   "\"\n"  (6 chars: quote, backslash, quote,
        # backslash, n, quote)
        closing_quote_lines = [
            ln for ln in lines if ln.strip() == r'"\"\n"'
        ]
        assert len(closing_quote_lines) >= 1, (
            "Expected at least one inner closing-quote line '\"\\n\"'"
        )
        # Verify it is the single-backslash form, not double-backslash.
        for ln in closing_quote_lines:
            stripped = ln.strip()
            assert stripped == r'"\"\n"', (
                f"Inner closing quote line should be '\"\\n\"' "
                f"(escaped quote + single backslash-n), got {stripped!r}"
            )
            assert stripped != r'"\\n"', (
                "Inner closing quote must NOT be double-escaped"
            )

    def test_simple_scalar_roundtrip(self):
        text = 'a: 1.0\nb: 2\nc: "hello"\nd: true\ne: TYPE_BOX\n'
        tree = parse_textproto(text)
        assert serialize_textproto(tree) == text

    def test_nested_block_roundtrip(self):
        text = (
            "outer {\n"
            "  inner {\n"
            "    x: 1.0\n"
            "    y: 2.0\n"
            "  }\n"
            "}\n"
        )
        tree = parse_textproto(text)
        assert serialize_textproto(tree) == text

    def test_go_roundtrip_byte_exact(self):
        """A full .go file (referenced + embedded components) round-trips.

        This pins the Level-1 adjacent-chunk emission: the embedded
        component's ``data:`` is rebuilt from its ``data_ast`` as per-line
        string literals terminated by the empty ``""`` sentinel.
        """
        tree = parse_textproto(
            GO_FULL_FIXTURE,
            schema={"components", "embedded_components"},
        )
        decoded = decode_embedded_data(tree)
        result = serialize_textproto(decoded)
        assert result == GO_FULL_FIXTURE

    def test_bare_ident_and_quoted_string_roundtrip(self):
        """Bare enum tokens stay unquoted; all-caps plain strings stay quoted.

        ``id: "HUD"`` is a string value and must round-trip quoted (an
        unquoted identifier would be invalid textproto for a string field).
        ``type: TYPE_BOX`` is a bare token and must round-trip unquoted.
        """
        text = 'id: "HUD"\ntype: TYPE_BOX\n'
        tree = parse_textproto(text)
        assert isinstance(tree["type"], BareIdent)
        assert isinstance(tree["id"], str) and not isinstance(tree["id"], BareIdent)
        assert serialize_textproto(tree) == text

    def test_control_char_scalar_roundtrip(self):
        """Tab and CR inside scalar strings re-emit as escapes, byte-exact."""
        text = r'x: "a\tb"' + "\n" + r'z: "cr\rlf"' + "\n"
        tree = parse_textproto(text)
        assert tree["x"] == "a\tb"
        assert tree["z"] == "cr\rlf"
        assert serialize_textproto(tree) == text

    def test_bare_ident_survives_decode(self):
        """decode_embedded_data's deepcopy preserves the BareIdent type."""
        text = (
            "embedded_components {\n"
            '  id: "sprite"\n'
            '  type: "sprite"\n'
            '  data: "shape: TYPE_BOX\n"\n'
            "}\n"
        )
        decoded = decode_embedded_data(parse_textproto(text))
        ast = decoded["embedded_components"]["data_ast"]
        assert isinstance(ast["shape"], BareIdent)
        assert ast["shape"] == "TYPE_BOX"

    def test_bare_ident_with_invalid_chars_emits_quoted(self):
        """A programmatically constructed BareIdent with non-ident characters
        must be emitted quoted and escaped, not raw."""
        # Valid bare ident: stays unquoted.
        tree_ok = {"k": BareIdent("TYPE_BOX")}
        assert serialize_textproto(tree_ok) == "k: TYPE_BOX\n"
        # Invalid bare ident (contains a double-quote): must be quoted.
        tree_bad = {"k": BareIdent('bad"value')}
        result = serialize_textproto(tree_bad)
        assert result == 'k: "bad\\"value"\n'


class TestMathDefaultOmission:
    """Zero/identity math components are omitted; floats always emit .0."""

    def test_position_omits_zero_components(self):
        tree = {"position": {"x": 200.0, "y": 0.0, "z": 0.0}}
        result = serialize_textproto(tree)
        assert result == "position {\n  x: 200.0\n}\n"

    def test_position_all_zero_omitted_entirely(self):
        tree = {"position": {"x": 0.0, "y": 0.0, "z": 0.0}}
        result = serialize_textproto(tree)
        assert result == ""

    def test_scale_all_ones_omitted_entirely(self):
        tree = {"scale": {"x": 1.0, "y": 1.0, "z": 1.0}}
        result = serialize_textproto(tree)
        assert result == ""

    def test_rotation_identity_omitted_entirely(self):
        tree = {"rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}
        result = serialize_textproto(tree)
        assert result == ""

    def test_scale_non_default_emits_only_non_default(self):
        tree = {"scale": {"x": 2.0, "y": 1.0, "z": 1.0}}
        result = serialize_textproto(tree)
        assert result == "scale {\n  x: 2.0\n}\n"

    def test_floats_always_emit_dot_zero(self):
        tree = {"value": 1.0}
        result = serialize_textproto(tree)
        assert result == "value: 1.0\n"

    def test_int_stays_int(self):
        tree = {"count": 42}
        result = serialize_textproto(tree)
        assert result == "count: 42\n"

    def test_negative_float(self):
        tree = {"value": -1.0}
        result = serialize_textproto(tree)
        assert result == "value: -1.0\n"


class TestGuardrails:
    """add_component guardrails and the embedded/referenced tree split."""

    def test_gui_embedded_raises(self):
        tree = {}
        with pytest.raises(ValueError) as exc_info:
            add_component(
                tree,
                component_id="my_gui",
                component_type="gui",
                embedded=True,
                data="some_gui_data",
            )
        assert str(exc_info.value) == (
            "GUI components (.gui) cannot be embedded inline; "
            "add as a referenced component"
        )

    def test_non_gui_non_embeddable_raises(self):
        tree = {}
        with pytest.raises(ValueError) as exc_info:
            add_component(
                tree,
                component_id="my_fx",
                component_type="particlefx",
                embedded=True,
                data="fx_data",
            )
        assert str(exc_info.value) == (
            "particlefx components cannot be embedded inline; "
            "add as a referenced component"
        )

    def test_gui_referenced_allowed(self):
        """The gui guardrail fires only for embedded=True."""
        tree = {}
        add_component(
            tree,
            component_id="my_gui",
            component_type="gui",
            embedded=False,
            data="/main/hero.gui",
        )
        assert tree["components"] == [
            {"id": "my_gui", "component": "/main/hero.gui"}
        ]

    def test_tilemap_normalised_to_tilegrid(self):
        tree = {}
        add_component(
            tree,
            component_id="my_tiles",
            component_type="tilemap",
            embedded=True,
            data="tile_data",
        )
        comp = tree["embedded_components"][0]
        assert comp["type"] == "tilegrid"
        assert comp["id"] == "my_tiles"

    def test_embedded_adds_to_embedded_components(self):
        tree = {}
        add_component(
            tree,
            component_id="sprite",
            component_type="sprite",
            embedded=True,
            data="sprite_data",
        )
        assert tree["embedded_components"] == [
            {"id": "sprite", "type": "sprite", "data": "sprite_data"}
        ]
        assert "components" not in tree

    def test_referenced_adds_to_components(self):
        """Referenced components carry a path, not a type."""
        tree = {}
        add_component(
            tree,
            component_id="script",
            component_type="script",
            embedded=False,
            data="/main/hero.script",
        )
        assert tree["components"] == [
            {"id": "script", "component": "/main/hero.script"}
        ]
        assert "embedded_components" not in tree

    def test_embedded_then_referenced_roundtrip(self):
        """An embedded add followed by a referenced add round-trips."""
        tree = {"id": "hero", "components": [], "embedded_components": []}
        add_component(
            tree,
            component_id="sprite",
            component_type="sprite",
            embedded=True,
            data='default_animation: "idle"\n',
        )
        add_component(
            tree,
            component_id="script",
            component_type="script",
            embedded=False,
            data="/main/hero.script",
        )
        result = serialize_textproto(tree)
        reparsed = parse_textproto(
            result, schema={"components", "embedded_components"}
        )
        assert reparsed["id"] == "hero"
        assert reparsed["components"] == [
            {"id": "script", "component": "/main/hero.script"}
        ]
        assert reparsed["embedded_components"] == [
            {
                "id": "sprite",
                "type": "sprite",
                "data": 'default_animation: "idle"\n',
            }
        ]

    def test_component_id_newline_raises(self):
        tree = {}
        with pytest.raises(ValueError):
            add_component(
                tree,
                component_id="bad\nid",
                component_type="sprite",
                embedded=True,
                data="d",
            )

    def test_component_type_newline_raises(self):
        tree = {}
        with pytest.raises(ValueError):
            add_component(
                tree,
                component_id="ok",
                component_type="bad\ntype",
                embedded=True,
                data="d",
            )

    def test_referenced_data_newline_raises(self):
        """A referenced component's data is a resource path: no newlines."""
        tree = {}
        with pytest.raises(ValueError):
            add_component(
                tree,
                component_id="ok",
                component_type="script",
                embedded=False,
                data="/bad\npath",
            )

    def test_embedded_data_newline_allowed(self):
        """Embedded data is proto content and stays unconstrained."""
        tree = {}
        add_component(
            tree,
            component_id="ok",
            component_type="sprite",
            embedded=True,
            data="line1\nline2",
        )
        assert tree["embedded_components"][0]["data"] == "line1\nline2"


def _make_test_png(width: int, height: int, alpha: int = 255) -> bytes:
    """Create a synthetic RGBA PNG and return its bytes."""
    img = Image.new("RGBA", (width, height), (255, 0, 0, alpha))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_rect_png(
    canvas_w: int,
    canvas_h: int,
    rect_w: int,
    rect_h: int,
    off_x: int,
    off_y: int,
) -> bytes:
    """A transparent canvas with one opaque ``rect_w`` x ``rect_h`` block.

    The block occupies pixels x in [off_x, off_x+rect_w) and y in
    [off_y, off_y+rect_h); every other pixel is fully transparent.
    """
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    for y in range(off_y, off_y + rect_h):
        for x in range(off_x, off_x + rect_w):
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCollisionFromImage:
    """collision_from_image generates TYPE_BOX and convex-hull shapes."""

    def test_box_mode(self):
        png_bytes = _make_test_png(100, 60)
        result = collision_from_image(png_bytes, mode="box")
        # Should contain TYPE_BOX with half-extents
        assert "TYPE_BOX" in result
        # width/2 = 50.0, height/2 = 30.0, z = 10.0
        assert "50.0" in result
        assert "30.0" in result
        assert "10.0" in result

    def test_convex_hull_mode(self):
        png_bytes = _make_test_png(100, 100)
        result = collision_from_image(png_bytes, mode="convex_hull")
        # Should contain vertices (x, y pairs)
        assert "vertices" in result
        # Box2D polygon limit: at most 16 vertices
        vertex_count = result.count("x:")
        assert vertex_count <= 16
        assert vertex_count >= 3

    def test_box_mode_non_square(self):
        png_bytes = _make_test_png(200, 50)
        result = collision_from_image(png_bytes, mode="box")
        assert "100.0" in result  # 200/2
        assert "25.0" in result   # 50/2

    def test_box_mode_partial_transparency(self):
        """Box half-extents track the opaque region, not the canvas.

        A 100x60 canvas with an opaque 21x11 rectangle at offset (10, 20)
        yields half-extents 10.5 and 5.5 (z stays 10.0).
        """
        png_bytes = _make_rect_png(100, 60, 21, 11, 10, 20)
        result = collision_from_image(png_bytes, mode="box")
        assert "TYPE_BOX" in result
        assert "x: 10.5" in result
        assert "y: 5.5" in result
        assert "z: 10.0" in result
        # The canvas dimensions must NOT leak into the box.
        assert "50.0" not in result
        assert "30.0" not in result

    def test_box_mode_empty_raises(self):
        png_bytes = _make_test_png(40, 40, alpha=0)
        with pytest.raises(ValueError):
            collision_from_image(png_bytes, mode="box")

    def test_convex_hull_mode_empty_raises(self):
        png_bytes = _make_test_png(40, 40, alpha=0)
        with pytest.raises(ValueError):
            collision_from_image(png_bytes, mode="convex_hull")

    def test_convex_hull_mode_collinear_raises(self):
        """Three collinear opaque pixels produce a degenerate 2-vertex hull."""
        # Draw a horizontal line of 3 pixels: all on the same y-axis.
        img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        for x in (5, 10, 15):
            img.putpixel((x, 10), (255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        with pytest.raises(ValueError, match="collinear"):
            collision_from_image(buf.getvalue(), mode="convex_hull")




# ---------------------------------------------------------------------------
# Part C: apply_operations registry
#
# Every op deep-copies its input and returns a NEW tree, so a failed batch
# never leaves a half-mutated resource behind. Unknown ops raise a ValueError
# naming the offending operation index.
# ---------------------------------------------------------------------------

import base64  # noqa: E402

from defold_mcp.textproto.mutations import apply_operations  # noqa: E402
from defold_mcp.textproto.schemas import ALWAYS_LIST_FIELDS  # noqa: E402


def _png_base64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


def _as_entries(value):
    """Normalise a repeated field the decoder may leave as a single dict."""
    return [value] if isinstance(value, dict) else value


class TestApplyOperations:
    # -- create -------------------------------------------------------------

    def test_create_go_seeds_component_lists(self):
        tree = apply_operations({}, [{"op": "create", "resource_type": "go"}])
        assert tree == {"components": [], "embedded_components": []}

    def test_create_other_type_is_an_empty_tree(self):
        tree = apply_operations(
            {}, [{"op": "create", "resource_type": "collection"}]
        )
        assert tree == {}

    def test_create_on_an_existing_tree_raises(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {"name": "x"}, [{"op": "create", "resource_type": "go"}]
            )

    # -- add_instance -------------------------------------------------------

    def test_add_instance(self):
        tree = apply_operations(
            {},
            [{"op": "add_instance", "id": "hero", "prototype": "/main/hero.go"}],
            extension=".collection",
        )
        assert tree["instances"] == [
            {"id": "hero", "prototype": "/main/hero.go"}
        ]

    def test_add_instance_with_transform(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "add_instance",
                    "id": "hero",
                    "prototype": "/main/hero.go",
                    "position": {"x": 1.0, "y": 2.0, "z": 0.0},
                    "scale": {"x": 2.0, "y": 2.0, "z": 1.0},
                }
            ],
            extension=".collection",
        )
        instance = tree["instances"][0]
        assert instance["position"] == {"x": 1.0, "y": 2.0, "z": 0.0}
        assert instance["scale"] == {"x": 2.0, "y": 2.0, "z": 1.0}

    # -- add_embedded_instance ---------------------------------------------

    def test_add_embedded_instance(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "add_embedded_instance",
                    "id": "hero",
                    "components": [
                        {
                            "id": "sprite",
                            "type": "sprite",
                            "data": 'default_animation: "idle"\n',
                        },
                        {"id": "script", "component": "/main/main.script"},
                    ],
                }
            ],
            extension=".collection",
        )
        entry = tree["embedded_instances"][0]
        assert entry["id"] == "hero"
        assert entry["data_ast"]["embedded_components"][0]["type"] == "sprite"
        assert (
            _as_entries(entry["data_ast"]["components"])[0]["component"]
            == "/main/main.script"
        )

    # -- add_subcollection --------------------------------------------------

    def test_add_subcollection_keeps_the_source_path(self):
        """``collection_instances.collection`` takes the SOURCE path.

        bob's ``CollectionBuilder`` resolves it with ``project.getResource``
        and never maps a ``.collectionc`` path back to its source, so
        appending ``c`` makes every headless build fail with "the collection
        ... can't be found" (verified against Defold 1.14.1, whose own test
        resources write ``collection: "/hierarchy/sub1.collection"``).
        """
        tree = apply_operations(
            {},
            [
                {
                    "op": "add_subcollection",
                    "id": "room",
                    "collection": "/main/room.collection",
                    "position": {"x": 10.0, "y": 0.0},
                }
            ],
            extension=".collection",
        )
        assert tree["collection_instances"] == [
            {
                "id": "room",
                "collection": "/main/room.collection",
                "position": {"x": 10.0, "y": 0.0},
            }
        ]

    # -- add_component ------------------------------------------------------

    def test_add_component_embedded_and_referenced(self):
        tree = apply_operations(
            {"components": [], "embedded_components": []},
            [
                {
                    "op": "add_component",
                    "id": "sprite",
                    "type": "sprite",
                    "embedded": True,
                    "data": 'material: "/m.material"\n',
                },
                {
                    "op": "add_component",
                    "id": "script",
                    "component": "/main/main.script",
                },
            ],
            extension=".go",
        )
        assert tree["embedded_components"][0]["id"] == "sprite"
        assert tree["components"][0] == {
            "id": "script",
            "component": "/main/main.script",
        }

    def test_add_component_non_embeddable_raises(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {"components": [], "embedded_components": []},
                [
                    {
                        "op": "add_component",
                        "id": "hud",
                        "type": "gui",
                        "embedded": True,
                        "data": "nodes {\n}\n",
                    }
                ],
                extension=".go",
            )

    # -- set_property -------------------------------------------------------

    def test_set_property_root_scalar(self):
        tree = apply_operations(
            {"name": "old"},
            [{"op": "set_property", "parent": "", "key": "name", "value": "new"}],
            extension=".collection",
        )
        assert tree["name"] == "new"

    def test_set_property_nested_dot_path(self):
        tree = apply_operations(
            {
                "embedded_components": [
                    {"id": "sprite", "data_ast": {"material": "/a.material"}}
                ]
            },
            [
                {
                    "op": "set_property",
                    "parent": "embedded_components.0.data_ast",
                    "key": "material",
                    "value": "/b.material",
                }
            ],
            extension=".go",
        )
        assert (
            tree["embedded_components"][0]["data_ast"]["material"]
            == "/b.material"
        )

    def test_set_property_rejects_message_values(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {},
                [
                    {
                        "op": "set_property",
                        "parent": "",
                        "key": "position",
                        "value": {"x": 1.0},
                    }
                ],
                extension=".collection",
            )

    # -- remove_child -------------------------------------------------------

    def test_remove_child_removes_the_first_match(self):
        tree = apply_operations(
            {"instances": [{"id": "a"}, {"id": "b"}, {"id": "a"}]},
            [
                {
                    "op": "remove_child",
                    "parent": "instances",
                    "match": {"key": "id", "value": "a"},
                }
            ],
            extension=".collection",
        )
        assert tree["instances"] == [{"id": "b"}, {"id": "a"}]

    def test_remove_child_without_a_match_raises(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {"instances": []},
                [
                    {
                        "op": "remove_child",
                        "parent": "instances",
                        "match": {"key": "id", "value": "nope"},
                    }
                ],
                extension=".collection",
            )

    # -- atlas --------------------------------------------------------------

    def test_atlas_add_images(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "atlas_add_images",
                    "images": ["/main/a.png", "/main/b.png"],
                }
            ],
            extension=".atlas",
        )
        assert tree["images"] == [
            {"image": "/main/a.png"},
            {"image": "/main/b.png"},
        ]

    def test_atlas_set_animation(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "atlas_set_animation",
                    "animation": {
                        "id": "idle",
                        "playback": "PLAYBACK_LOOP_FORWARD",
                        "fps": 12,
                    },
                }
            ],
            extension=".atlas",
        )
        assert tree["animations"] == [
            {"id": "idle", "playback": "PLAYBACK_LOOP_FORWARD", "fps": 12}
        ]

    # -- tilesource ---------------------------------------------------------

    def test_tilesource_configure(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "tilesource_configure",
                    "keys": {
                        "image": "/main/tiles.png",
                        "tile_width": 16,
                        "tile_height": 16,
                        "margin": 0,
                        "spacing": 0,
                    },
                }
            ],
            extension=".tilesource",
        )
        assert tree["tile_width"] == 16
        assert tree["image"] == "/main/tiles.png"

    # -- tilemap ------------------------------------------------------------

    def test_tilemap_set_tiles_by_layer_id(self):
        tree = apply_operations(
            {"layer_grids": [{"id": "layer1"}]},
            [
                {
                    "op": "tilemap_set_tiles",
                    "layer": "layer1",
                    "rows": [["1,2,0"], ["3,4,0"]],
                }
            ],
            extension=".tilemap",
        )
        assert tree["layer_grids"][0]["data"] == [[1, 2, 0], [3, 4, 0]]

    # -- gui ----------------------------------------------------------------

    def test_gui_add_node(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "gui_add_node",
                    "node": {
                        "id": "label",
                        "position": {"x": 10.0, "y": 10.0, "z": 0.0},
                        "size": {"x": 100.0, "y": 40.0, "z": 0.0},
                        "text": "hello",
                    },
                }
            ],
            extension=".gui",
        )
        node = tree["nodes"][0]
        assert node["id"] == "label"
        assert node["text"] == "hello"

    def test_gui_set_layout_property_on_existing_node(self):
        tree = apply_operations(
            {"layouts": [{"id": "landscape", "nodes": [{"id": "label"}]}]},
            [
                {
                    "op": "gui_set_layout_property",
                    "layout_id": "landscape",
                    "node_id": "label",
                    "key": "x",
                    "value": 5.0,
                }
            ],
            extension=".gui",
        )
        assert tree["layouts"][0]["nodes"][0]["x"] == 5.0

    def test_gui_set_layout_property_appends_missing_entries(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "gui_set_layout_property",
                    "layout_id": "portrait",
                    "node_id": "label",
                    "key": "x",
                    "value": 1.0,
                }
            ],
            extension=".gui",
        )
        assert tree["layouts"][0]["id"] == "portrait"
        assert tree["layouts"][0]["nodes"][0] == {"id": "label", "x": 1.0}

    # -- collision ----------------------------------------------------------

    def test_collision_add_shape_explicit(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "collision_add_shape",
                    "shape": {
                        "shape_type": "TYPE_BOX",
                        "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                    },
                }
            ],
            extension=".collisionobject",
        )
        assert tree["shapes"][0]["type"] == "TYPE_BOX"

    def test_collision_add_shape_from_image(self):
        png = _make_rect_png(100, 60, 21, 11, 10, 20)
        tree = apply_operations(
            {},
            [
                {
                    "op": "collision_add_shape",
                    "image_png_base64": _png_base64(png),
                    "mode": "box",
                }
            ],
            extension=".collisionobject",
        )
        assert tree["shapes"][0]["type"] == "TYPE_BOX"

    # -- guards -------------------------------------------------------------

    def test_unknown_op_names_the_index(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations({}, [{"op": "bogus"}], extension=".collection")

    def test_unknown_op_at_a_later_index(self):
        with pytest.raises(ValueError, match="operation 1"):
            apply_operations(
                {},
                [{"op": "atlas_add_images", "images": []}, {"op": "bogus"}],
                extension=".atlas",
            )

    def test_input_tree_is_not_mutated(self):
        original = {"instances": []}
        apply_operations(
            original,
            [{"op": "add_instance", "id": "x", "prototype": "/x.go"}],
            extension=".collection",
        )
        assert original == {"instances": []}

    def test_multiline_string_argument_is_rejected(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {},
                [
                    {
                        "op": "add_instance",
                        "id": "bad\nid",
                        "prototype": "/x.go",
                    }
                ],
                extension=".collection",
            )

    # -- end to end ---------------------------------------------------------

    def test_end_to_end_go_and_collection_round_trip(self):
        go_tree = apply_operations(
            {},
            [
                {"op": "create", "resource_type": "go"},
                {
                    "op": "add_component",
                    "id": "sprite",
                    "type": "sprite",
                    "embedded": True,
                    "data": (
                        'default_animation: "idle"\n'
                        'material: "/builtins/materials/sprite.material"\n'
                    ),
                },
                {
                    "op": "add_component",
                    "id": "script",
                    "component": "/main/main.script",
                },
            ],
            extension=".go",
        )
        go_text = serialize_textproto(go_tree)
        go_reparsed = parse_textproto(go_text, schema=ALWAYS_LIST_FIELDS)
        assert (
            serialize_textproto(decode_embedded_data(go_reparsed)) == go_text
        )
        assert go_reparsed["embedded_components"][0]["id"] == "sprite"
        assert (
            go_reparsed["components"][0]["component"] == "/main/main.script"
        )

        collection_tree = apply_operations(
            {"name": "main", "scale_along_z": 0},
            [{"op": "add_instance", "id": "hero", "prototype": "/main/hero.go"}],
            extension=".collection",
        )
        collection_text = serialize_textproto(collection_tree)
        collection_reparsed = parse_textproto(
            collection_text, schema=ALWAYS_LIST_FIELDS
        )
        assert (
            serialize_textproto(decode_embedded_data(collection_reparsed))
            == collection_text
        )
        assert (
            collection_reparsed["instances"][0]["prototype"] == "/main/hero.go"
        )


class TestSchemaOrderedSerialisation:
    """``extension=`` reorders top-level keys; the default preserves order."""

    def test_extension_emits_canonical_top_level_order(self):
        tree = {
            "embedded_instances": [{"id": "hero", "data": "a: 1\n"}],
            "scale_along_z": 0,
            "name": "main",
        }
        text = serialize_textproto(tree, extension=".collection")
        # COLLECTION_FIELDS order is name, instances, collection_instances,
        # scale_along_z, embedded_instances, embedded_components.
        assert text.index("name:") < text.index("scale_along_z:")
        assert text.index("scale_along_z:") < text.index("embedded_instances")

    def test_unknown_extension_leaves_the_order_alone(self):
        tree = {"scale_along_z": 0, "name": "main"}
        text = serialize_textproto(tree, extension=".nonsense")
        assert text.index("scale_along_z:") < text.index("name:")

    def test_default_extension_preserves_insertion_order(self):
        tree = {"scale_along_z": 0, "name": "main"}
        text = serialize_textproto(tree)
        assert text.index("scale_along_z:") < text.index("name:")

    def test_phase2_fixture_round_trips_without_extension(self):
        tree = parse_textproto(GO_FIXTURE, schema=ALWAYS_LIST_FIELDS)
        assert serialize_textproto(decode_embedded_data(tree)) == GO_FIXTURE

    def test_phase2_collection_fixture_round_trips_without_extension(self):
        tree = parse_textproto(COLLECTION_FIXTURE, schema=ALWAYS_LIST_FIELDS)
        assert (
            serialize_textproto(decode_embedded_data(tree))
            == COLLECTION_FIXTURE
        )


class TestApplyOperationsGuards:
    """String-argument guards that every op must apply uniformly."""

    def test_atlas_set_animation_rejects_a_multiline_image(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {},
                [
                    {
                        "op": "atlas_set_animation",
                        "animation": {
                            "id": "idle",
                            "images": ["ok", "bad\nimage"],
                        },
                    }
                ],
                extension=".atlas",
            )

    def test_atlas_set_animation_accepts_plain_images(self):
        tree = apply_operations(
            {},
            [
                {
                    "op": "atlas_set_animation",
                    "animation": {"id": "idle", "images": ["a", "b"]},
                }
            ],
            extension=".atlas",
        )
        assert tree["animations"][0]["images"] == ["a", "b"]

    def test_add_component_embedded_requires_a_type(self):
        with pytest.raises(ValueError, match="operation 0"):
            apply_operations(
                {"components": [], "embedded_components": []},
                [
                    {
                        "op": "add_component",
                        "id": "sprite",
                        "embedded": True,
                        "data": 'material: "/m.material"\n',
                    }
                ],
                extension=".go",
            )
