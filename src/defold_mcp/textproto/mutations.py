"""Resource mutators for Defold textproto trees.

Provides ``add_component`` (with guardrails for non-embeddable types),
``collision_from_image`` (box and convex-hull collision shapes from a PNG),
and the :func:`apply_operations` registry the ``defold_mutate_resource``
tool dispatches through.
"""

from __future__ import annotations

import base64
import copy
import io
import math
from typing import Any

from PIL import Image

from .parser import decode_embedded_data, parse_textproto
from .schemas import EMBED_TYPE_NORMALISATION, NON_EMBEDDABLE_TYPES
from .serializer import serialize_textproto

__all__ = ["add_component", "apply_operations", "collision_from_image"]

# Fixed half-depth (z) for every generated collision box.
_BOX_HALF_DEPTH = 10.0
# Box2D polygon vertex limit.
_MAX_HULL_VERTICES = 16
# Alpha threshold (0-255) above which a pixel counts as opaque.
_ALPHA_THRESHOLD = 127


def add_component(
    tree: dict,
    *,
    component_id: str,
    component_type: str,
    embedded: bool,
    data: str,
) -> None:
    """Append a component to the correct list in ``tree``.

    The embedded/referenced split mirrors Defold's ``.go`` layout:

    * ``embedded=True`` appends ``{"id", "type", "data"}`` to
      ``tree["embedded_components"]`` (created if absent). ``data`` is the
      inline component proto text.
    * ``embedded=False`` appends ``{"id", "component"}`` to
      ``tree["components"]`` (created if absent), where ``data`` is the
      component's resource path (e.g. ``/main/hero.script``). Referenced
      components carry no ``type`` field, so ``component_type`` is not stored
      in this mode.

    Guardrails (embedded components only):
      * Non-embeddable types (see ``NON_EMBEDDABLE_TYPES``) may not be added
        with ``embedded=True``; a ``ValueError`` is raised instead.
      * Type names listed in ``EMBED_TYPE_NORMALISATION`` (e.g. ``tilemap``)
        are rewritten to their canonical form (e.g. ``tilegrid``).
    """
    # Guard against control characters that would corrupt the serialised
    # output (a raw newline inside a quoted id/path is invalid textproto).
    for label, val in (
        ("component_id", component_id),
        ("component_type", component_type),
    ):
        if "\n" in val or "\r" in val:
            raise ValueError(
                f"{label} must not contain newline or carriage-return"
            )
    if not embedded and ("\n" in data or "\r" in data):
        raise ValueError(
            "referenced component data (resource path) must not contain "
            "newline or carriage-return"
        )

    if embedded:
        if component_type in NON_EMBEDDABLE_TYPES:
            if component_type == "gui":
                raise ValueError(
                    "GUI components (.gui) cannot be embedded inline; "
                    "add as a referenced component"
                )
            raise ValueError(
                f"{component_type} components cannot be embedded inline; "
                "add as a referenced component"
            )
        component_type = EMBED_TYPE_NORMALISATION.get(
            component_type, component_type
        )
        tree.setdefault("embedded_components", []).append(
            {"id": component_id, "type": component_type, "data": data}
        )
    else:
        tree.setdefault("components", []).append(
            {"id": component_id, "component": data}
        )


def collision_from_image(png_bytes: bytes, *, mode: str = "box") -> str:
    """Return a collision-object textproto fragment for a PNG.

    Both modes derive their geometry from the NON-TRANSPARENT region of the
    image (pixels whose alpha exceeds ``_ALPHA_THRESHOLD``), not the canvas
    size. An image with no opaque pixels raises ``ValueError`` in either mode.

    ``mode="box"`` yields a ``TYPE_BOX`` whose half-extents are half the
    opaque region's bounding-box width and height, with a fixed ``z`` of
    10.0. ``mode="convex_hull"`` yields a convex hull of the opaque pixels,
    origin-centred, y-up, with at most 16 vertices and ``z = 0.0``.
    """
    img = Image.open(io.BytesIO(png_bytes))
    width, height = img.size
    opaque = _opaque_pixels(img, width, height)
    if mode == "box":
        return _box_fragment(opaque)
    if mode == "convex_hull":
        return _hull_fragment(opaque)
    raise ValueError(f"Unknown collision mode: {mode!r}")


def _opaque_pixels(
    img: Image.Image, width: int, height: int
) -> list[tuple[int, int]]:
    """All ``(x, y)`` pixels whose alpha exceeds the opaque threshold."""
    pixels = img.convert("RGBA").load()
    return [
        (x, y)
        for y in range(height)
        for x in range(width)
        if pixels[x, y][3] > _ALPHA_THRESHOLD
    ]


def _box_fragment(opaque: list[tuple[int, int]]) -> str:
    """``TYPE_BOX`` fragment sized to the opaque region's bounding box."""
    if not opaque:
        raise ValueError("No opaque pixels found; cannot derive a box")
    xs = [p[0] for p in opaque]
    ys = [p[1] for p in opaque]
    width = max(xs) - min(xs) + 1
    height = max(ys) - min(ys) + 1
    return (
        "type: TYPE_BOX\n"
        "box {\n"
        f"  x: {width / 2}\n"
        f"  y: {height / 2}\n"
        f"  z: {_BOX_HALF_DEPTH}\n"
        "}\n"
    )


def _hull_fragment(opaque: list[tuple[int, int]]) -> str:
    """Convex-hull fragment: opaque pixels, origin-centred, y-up, CCW."""
    if len(opaque) < 3:
        raise ValueError("Not enough opaque pixels to form a convex hull")

    hull = _convex_hull(opaque)
    if len(hull) < 3:
        raise ValueError(
            "opaque pixels are collinear; cannot form a convex hull"
        )
    cx = (min(p[0] for p in hull) + max(p[0] for p in hull)) / 2
    cy = (min(p[1] for p in hull) + max(p[1] for p in hull)) / 2
    # Flip to Defold y-up (y' = cy - y) and centre on the origin (x' = x - cx).
    vertices = _ensure_ccw(_reduce_vertices(
        [(x - cx, cy - y) for x, y in hull], _MAX_HULL_VERTICES
    ))

    parts = ["type: TYPE_CONVEX_HULL\n"]
    for x, y in vertices:
        parts.append(f"vertices {{\n  x: {x}\n  y: {y}\n  z: 0.0\n}}\n")
    return "".join(parts)


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone-chain convex hull of a set of points."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _ensure_ccw(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return ``pts`` in counter-clockwise order (positive signed area)."""
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return list(reversed(pts)) if area < 0 else list(pts)


def _reduce_vertices(
    pts: list[tuple[float, float]], max_vertices: int
) -> list[tuple[float, float]]:
    """Drop the least-significant vertices until at most ``max_vertices``."""
    pts = list(pts)
    while len(pts) > max_vertices:
        n = len(pts)
        min_dist, min_idx = None, 0
        for i in range(n):
            d = _perp_distance(pts[(i - 1) % n], pts[i], pts[(i + 1) % n])
            if min_dist is None or d < min_dist:
                min_dist, min_idx = d, i
        pts.pop(min_idx)
    return pts


def _perp_distance(a, b, c) -> float:
    """Perpendicular distance from ``b`` to the line through ``a`` and ``c``."""
    dx, dy = c[0] - a[0], c[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return 0.0
    return abs(dy * b[0] - dx * b[1] + c[0] * a[1] - c[1] * a[0]) / length


# ---------------------------------------------------------------------------
# apply_operations registry
#
# Every op is a plain function ``(tree, op, extension) -> None`` registered
# in :data:`_OP_REGISTRY`. The tree is deep-copied on entry, so a batch is
# atomic: a failure part-way through leaves the caller's tree untouched.
# ---------------------------------------------------------------------------

# Path segments in a ``set_property`` / ``remove_child`` ``parent`` are
# separated by dots; a segment that is all digits indexes a list.
_MATH_KEYS = ("position", "rotation", "scale")


def apply_operations(
    tree: dict,
    operations: list[dict],
    *,
    extension: str | None = None,
) -> dict:
    """Apply a batch of mutation ops to a parsed textproto tree.

    ``tree`` is the current tree of the target resource (``{}`` when the
    resource does not exist yet). ``operations`` is a list of op dicts, each
    naming its action in the ``"op"`` key; ``extension`` is the resource's
    file extension (e.g. ``".collection"``) and selects the fresh-tree shape
    for ``create``.

    The input tree is never mutated: ops run against a deep copy which is
    returned. The result is passed through ``decode_embedded_data`` so that
    a freshly built resource is already in the canonical parsed shape (every
    embedded ``data`` payload carries its ``data_ast``), which is what makes
    a freshly written resource round-trip byte-exactly. Any failure raises
    ``ValueError`` naming the offending operation index, so a bad batch
    cannot half-apply.

    Supported ops: ``create``, ``add_instance``, ``add_embedded_instance``,
    ``add_subcollection``, ``add_component``, ``set_property``,
    ``remove_child``, ``atlas_add_images``, ``atlas_set_animation``,
    ``tilesource_configure``, ``tilemap_set_tiles``, ``gui_add_node``,
    ``gui_set_layout_property``, ``collision_add_shape``.
    """
    result = copy.deepcopy(tree)
    for index, op in enumerate(operations):
        name = op.get("op") if isinstance(op, dict) else None
        handler = _OP_REGISTRY.get(name) if isinstance(name, str) else None
        if handler is None:
            raise ValueError(f"operation {index}: unknown op {name!r}")
        try:
            handler(result, op, extension)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"operation {index} ({name}): {exc}") from exc
    return decode_embedded_data(result)


# -- shared helpers ---------------------------------------------------------


def _single_line(value: object, label: str) -> str:
    """Return ``value`` as a single-line string or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must not contain a newline")
    return value


def _scalar(value: object, label: str) -> object:
    """Reject message (dict/list) values; validate string values."""
    if isinstance(value, (dict, list, tuple)):
        raise ValueError(
            f"{label} must be a scalar (message values need their own op)"
        )
    if isinstance(value, str):
        _single_line(value, label)
    return value


def _as_list(node: dict, key: str) -> list:
    """Return ``node[key]`` as a list, creating it when absent."""
    value = node.get(key)
    if value is None:
        value = []
        node[key] = value
    elif not isinstance(value, list):
        value = [value]
        node[key] = value
    return value


def _math(op: dict, key: str) -> dict | None:
    """Extract an optional transform block (``position``/``rotation``/``scale``)."""
    if key not in op:
        return None
    value = op[key]
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a message with x/y/z (and w) fields")
    return value


def _navigate(tree: dict, parent: str) -> object:
    """Resolve a dotted ``parent`` path (``""`` = the tree root)."""
    node: object = tree
    if not parent:
        return node
    for segment in parent.split("."):
        if isinstance(node, dict):
            if segment not in node:
                raise ValueError(f"parent path {parent!r} has no {segment!r}")
            node = node[segment]
        elif isinstance(node, list):
            if not segment.isdigit() or int(segment) >= len(node):
                raise ValueError(
                    f"parent path {parent!r}: {segment!r} is not a valid index"
                )
            node = node[int(segment)]
        else:
            raise ValueError(
                f"parent path {parent!r} descends into a scalar at {segment!r}"
            )
    return node


# -- op handlers ------------------------------------------------------------


def _op_create(tree: dict, op: dict, extension: str | None) -> None:
    if tree:
        raise ValueError(
            "create: the resource already exists (tree is not empty)"
        )
    resource_type = op.get("resource_type") or (
        extension.lstrip(".") if extension else ""
    )
    if resource_type == "go":
        tree["components"] = []
        tree["embedded_components"] = []


def _op_add_instance(tree: dict, op: dict, extension: str | None) -> None:
    entry: dict = {
        "id": _single_line(op["id"], "id"),
        "prototype": _single_line(op["prototype"], "prototype"),
    }
    for key in _MATH_KEYS:
        block = _math(op, key)
        if block is not None:
            entry[key] = block
    _as_list(tree, "instances").append(entry)


def _op_add_embedded_instance(
    tree: dict, op: dict, extension: str | None
) -> None:
    inner: dict = {"components": [], "embedded_components": []}
    for component in op.get("components") or []:
        component_id = _single_line(component["id"], "component id")
        if component.get("type"):
            inner["embedded_components"].append(
                {
                    "id": component_id,
                    "type": _single_line(component["type"], "component type"),
                    "data": component.get("data", ""),
                }
            )
        else:
            inner["components"].append(
                {
                    "id": component_id,
                    "component": _single_line(
                        component["component"], "component path"
                    ),
                }
            )
    entry: dict = {
        "id": _single_line(op["id"], "id"),
        "data": serialize_textproto(inner),
        "data_ast": inner,
    }
    block = _math(op, "position")
    if block is not None:
        entry["position"] = block
    _as_list(tree, "embedded_instances").append(entry)


def _op_add_subcollection(
    tree: dict, op: dict, extension: str | None
) -> None:
    # ``collection_instances.collection`` carries the SOURCE path
    # (``/hierarchy/sub1.collection``), never the compiled one: bob's
    # CollectionBuilder resolves it with ``project.getResource`` and does
    # not map ``.collectionc`` back to its source, so appending ``c`` makes
    # every build fail with "the collection ... can't be found" (verified
    # against Defold 1.14.1).
    entry: dict = {
        "id": _single_line(op["id"], "id"),
        "collection": _single_line(op["collection"], "collection"),
    }
    block = _math(op, "position")
    if block is not None:
        entry["position"] = block
    _as_list(tree, "collection_instances").append(entry)


def _op_add_component(tree: dict, op: dict, extension: str | None) -> None:
    embedded = bool(op.get("embedded", False))
    if embedded:
        component_type = op.get("type")
        if not component_type:
            raise ValueError(
                "embedded add_component requires a non-empty 'type'"
            )
        add_component(
            tree,
            component_id=_single_line(op["id"], "id"),
            component_type=_single_line(component_type, "type"),
            embedded=True,
            data=op.get("data", ""),
        )
    else:
        add_component(
            tree,
            component_id=_single_line(op["id"], "id"),
            component_type="",
            embedded=False,
            data=_single_line(op["component"], "component"),
        )


def _op_set_property(tree: dict, op: dict, extension: str | None) -> None:
    target = _navigate(tree, op.get("parent", ""))
    if not isinstance(target, dict):
        raise ValueError("parent must resolve to a message, not a list")
    key = _single_line(op["key"], "key")
    target[key] = _scalar(op["value"], "value")


def _op_remove_child(tree: dict, op: dict, extension: str | None) -> None:
    target = _navigate(tree, op.get("parent", ""))
    if not isinstance(target, list):
        raise ValueError("parent must resolve to a repeated field (a list)")
    match = op["match"]
    match_key = _single_line(match["key"], "match key")
    match_value = match["value"]
    for index, item in enumerate(target):
        if isinstance(item, dict) and item.get(match_key) == match_value:
            del target[index]
            return
    raise ValueError(
        f"no child matching {match_key}={match_value!r} under "
        f"{op.get('parent', '')!r}"
    )


def _op_atlas_add_images(tree: dict, op: dict, extension: str | None) -> None:
    images = _as_list(tree, "images")
    for path in op["images"]:
        images.append({"image": _single_line(path, "image")})


def _op_atlas_set_animation(
    tree: dict, op: dict, extension: str | None
) -> None:
    animation = op["animation"]
    entry: dict = {"id": _single_line(animation["id"], "animation id")}
    if "playback" in animation:
        entry["playback"] = _single_line(
            animation["playback"], "playback"
        )
    if "images" in animation:
        entry["images"] = [
            _single_line(image, "animation image")
            for image in animation["images"]
        ]
    if "fps" in animation:
        entry["fps"] = animation["fps"]
    _as_list(tree, "animations").append(entry)


def _op_tilesource_configure(
    tree: dict, op: dict, extension: str | None
) -> None:
    for key, value in op["keys"].items():
        tree[_single_line(key, "key")] = _scalar(value, f"keys.{key}")


def _op_tilemap_set_tiles(
    tree: dict, op: dict, extension: str | None
) -> None:
    field = "layer_grids" if "layer_grids" in tree else "layers"
    entries = _as_list(tree, field)
    layer = op["layer"]
    target: dict | None = None
    if isinstance(layer, int) and not isinstance(layer, bool):
        if 0 <= layer < len(entries) and isinstance(entries[layer], dict):
            target = entries[layer]
    else:
        layer_id = _single_line(layer, "layer")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == layer_id:
                target = entry
                break
    if target is None:
        target = {} if isinstance(layer, int) else {"id": layer}
        entries.append(target)
    target["data"] = [_row_to_ints(row) for row in op["rows"]]


def _row_to_ints(row: object) -> list[int]:
    """Flatten one tilemap row (``["1,2,0"]``) into a list of ints."""
    if not isinstance(row, (list, tuple)):
        raise ValueError("each row must be a list of comma-separated cells")
    cells: list[int] = []
    for cell in row:
        for part in str(cell).split(","):
            part = part.strip()
            if part:
                cells.append(int(part))
    return cells


def _op_gui_add_node(tree: dict, op: dict, extension: str | None) -> None:
    node = op["node"]
    entry: dict = {"id": _single_line(node["id"], "node id")}
    for key in ("position", "size", "text"):
        if key in node:
            entry[key] = node[key]
    _as_list(tree, "nodes").append(entry)


def _op_gui_set_layout_property(
    tree: dict, op: dict, extension: str | None
) -> None:
    layout_id = _single_line(op["layout_id"], "layout_id")
    node_id = _single_line(op["node_id"], "node_id")
    key = _single_line(op["key"], "key")

    layout: dict | None = None
    for candidate in _as_list(tree, "layouts"):
        if isinstance(candidate, dict) and candidate.get("id") == layout_id:
            layout = candidate
            break
    if layout is None:
        layout = {"id": layout_id, "nodes": []}
        tree.setdefault("layouts", []).append(layout)

    nodes = _as_list(layout, "nodes")
    target: dict | None = None
    for candidate in nodes:
        if isinstance(candidate, dict) and candidate.get("id") == node_id:
            target = candidate
            break
    if target is None:
        target = {"id": node_id}
        nodes.append(target)
    target[key] = _scalar(op["value"], "value")


def _op_collision_add_shape(
    tree: dict, op: dict, extension: str | None
) -> None:
    shapes = _as_list(tree, "shapes")
    if "shape" in op:
        shape = op["shape"]
        entry: dict = {
            # The op names the field ``shape_type``; Defold's proto field is
            # ``type`` inside a ``shapes`` entry.
            "type": _single_line(shape["shape_type"], "shape_type"),
        }
        for key in ("position", "vertices"):
            if key in shape:
                entry[key] = shape[key]
        shapes.append(entry)
        return
    png = base64.b64decode(op["image_png_base64"])
    fragment = collision_from_image(png, mode=op.get("mode", "box"))
    shapes.append(parse_textproto(fragment))


_OP_REGISTRY = {
    "create": _op_create,
    "add_instance": _op_add_instance,
    "add_embedded_instance": _op_add_embedded_instance,
    "add_subcollection": _op_add_subcollection,
    "add_component": _op_add_component,
    "set_property": _op_set_property,
    "remove_child": _op_remove_child,
    "atlas_add_images": _op_atlas_add_images,
    "atlas_set_animation": _op_atlas_set_animation,
    "tilesource_configure": _op_tilesource_configure,
    "tilemap_set_tiles": _op_tilemap_set_tiles,
    "gui_add_node": _op_gui_add_node,
    "gui_set_layout_property": _op_gui_set_layout_property,
    "collision_add_shape": _op_collision_add_shape,
}
