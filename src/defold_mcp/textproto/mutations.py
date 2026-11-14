"""Resource mutators for Defold textproto trees.

Provides ``add_component`` (with guardrails for non-embeddable types) and
``collision_from_image`` (box and convex-hull collision shapes from a PNG).
"""

from __future__ import annotations

import io
import math

from PIL import Image

from .schemas import EMBED_TYPE_NORMALISATION, NON_EMBEDDABLE_TYPES

__all__ = ["add_component", "collision_from_image"]

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
