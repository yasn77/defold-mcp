"""Field ordering, defaults and embeddability rules for Defold resources.

These tables drive the canonical serializer (field order, always-list
normalisation) and the high-level mutators (which component types may be
embedded inline, and which type names must be normalised).

Field orders follow the Defold Editor's own write order for each message
type, so that a parsed-then-serialised resource is byte-stable.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical field orders (Defold Editor write order per message type)
#
# NOTE: These tables and ALWAYS_LIST_FIELDS are NOT yet wired into the
# serializer (which follows dict insertion order). They MUST be wired in
# the server-wiring task (Task 11) before any freshly-created resource is
# serialised. PROTOTYPE_FIELDS' leading-id question (whether ``id`` precedes
# ``components``) is to be settled from real ``.go`` fixtures then.
# ---------------------------------------------------------------------------

COLLECTION_FIELDS: tuple[str, ...] = (
    "name",
    "instances",
    "collection_instances",
    "scale_along_z",
    "embedded_instances",
    "embedded_components",
)

PROTOTYPE_FIELDS: tuple[str, ...] = (
    "components",
    "embedded_components",
)

ATLAS_FIELDS: tuple[str, ...] = (
    "name",
    "size",
    "images",
    "animations",
)

TILESOURCE_FIELDS: tuple[str, ...] = (
    "name",
    "images",
    "tile_grids",
)

TILEGRID_FIELDS: tuple[str, ...] = (
    "name",
    "source",
    "tile_size",
    "margin",
    "spacing",
)

SCENE_FIELDS: tuple[str, ...] = (
    "name",
    "nodes",
    "layouts",
)

COLLISIONOBJECT_FIELDS: tuple[str, ...] = (
    "type",
    "vertices",
    "box",
    "friction",
    "restitution",
    "mass",
    "inv_mass",
    "inertia",
    "inv_inertia",
)

MATERIAL_FIELDS: tuple[str, ...] = (
    "name",
    "vertex",
    "fragment",
    "textures",
)

# Mapping from a resource file extension to its top-level field order.
SCHEMA_BY_EXTENSION: dict[str, tuple[str, ...]] = {
    ".collection": COLLECTION_FIELDS,
    ".go": PROTOTYPE_FIELDS,
    ".atlas": ATLAS_FIELDS,
    ".tilesource": TILESOURCE_FIELDS,
    ".tilegrid": TILEGRID_FIELDS,
    ".gui": SCENE_FIELDS,
    ".collisionobject": COLLISIONOBJECT_FIELDS,
    ".material": MATERIAL_FIELDS,
}

# ---------------------------------------------------------------------------
# Repeated fields that must ALWAYS be lists, even when they occur exactly
# once, so downstream mutation is predictable.
# ---------------------------------------------------------------------------

ALWAYS_LIST_FIELDS: frozenset[str] = frozenset(
    {
        "instances",
        "collection_instances",
        "embedded_instances",
        "embedded_components",
        "components",
        "images",
        "animations",
        "nodes",
        "layouts",
        "vertices",
    }
)

# ---------------------------------------------------------------------------
# Embeddability rules for inline components (embedded_components /
# embedded_instances). These component types must be referenced by path
# (components { id: "..." component: "/path" }) and cannot be inlined.
# ---------------------------------------------------------------------------

NON_EMBEDDABLE_TYPES: frozenset[str] = frozenset(
    {
        "gui",
        "script",
        "particlefx",
    }
)

# Type-name normalisation applied when adding an inline component. An inline
# tilemap is written as type "tilegrid" in Defold, never "tilemap".
EMBED_TYPE_NORMALISATION: dict[str, str] = {
    "tilemap": "tilegrid",
}

# ---------------------------------------------------------------------------
# Math-default omission
# ---------------------------------------------------------------------------

# Fields whose all-default message blocks are omitted entirely, and the
# per-component default value used to decide which components to drop.
#   position / size : default 0.0
#   scale           : default 1.0
#   rotation        : identity quaternion (0, 0, 0, 1)
MATH_DEFAULTS: dict[str, dict[str, float]] = {
    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    # Provisional: inclusion beyond the plan's position/scale/rotation list —
    # verify against a real .gui fixture before wiring .gui support (Task 11).
    "size": {"x": 0.0, "y": 0.0, "z": 0.0},
    "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
    "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
}

# A position/size block that carries all three of x, y and z and is entirely
# at its default is omitted wholesale; a partial block (e.g. a 2D position
# with only x and y) is kept verbatim even when every component is default,
# because the absence of z is itself meaningful in a 2D scene.
_FULL_TRIPLE = ("x", "y", "z")
