---
type: architecture
title: Mutations and collision shapes
description: The textproto mutation layer — add_component's embedded/referenced split with guardrails, and PNG-derived box and convex-hull collision shape generation.
tags: [defold, textproto, mutations, collision, png]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-14T22:05:56.160Z
sources:
  - id: openwiki-source-ce941463b68b754d5210fcd3
    resource: repo://src/defold_mcp/textproto/mutations.py
  - id: openwiki-source-920c288fa775427e5500aec9
    resource: repo://src/defold_mcp/textproto/schemas.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-14T22:05:56.160Z" }
---

# Mutations and collision shapes

`src/defold_mcp/textproto/mutations.py` sits above the [textproto engine](textproto-engine.md) and below the MCP tool layer. It owns the two operations that need real logic rather than tree plumbing: adding components to game objects, and generating physics collision shapes from images. It is deliberately thin — an operations dispatch registry arrives with the server-wiring task, which will route `defold_mutate_resource` operations to these functions and further mutators.

## `add_component`

Appends a component to a `.go`-shaped tree, honouring the real Defold split between referenced and embedded components:

- **Embedded** (`embedded=True`): appends `{"id", "type", "data"}` to `tree["embedded_components"]` — matching the on-disk form `embedded_components { id: "sprite" type: "sprite" data: "…" }`.
- **Referenced** (`embedded=False`): appends `{"id", "component"}` to `tree["components"]` — matching `components { id: "script" component: "/main/hero.script" }`. The `component_type` argument is accepted but not persisted: referenced components carry no `type` field in Defold's PrototypeDesc.

### Guardrails

- **Non-embeddable types.** `embedded=True` with a type in `NON_EMBEDDABLE_TYPES` (`gui`, `script`, `particlefx`) raises `ValueError`. For `gui` the message is exact: `GUI components (.gui) cannot be embedded inline; add as a referenced component`. The set is deliberately conservative: the plan only names `.gui` as definitely illegal, and the failure mode of over-restriction is a clear, actionable error while under-restriction would produce an editor-rejected file.
- **Type normalisation.** `tilemap` is rewritten to `tilegrid` via `EMBED_TYPE_NORMALISATION` (an inline tilemap component is written as type `tilegrid` in Defold). The normalisation applies on the embedded path.
- **Injection guards.** `component_id`, `component_type`, and the referenced-mode `data` (a resource path) reject `\n`/`\r` with `ValueError` before any mutation — mirroring the `project.py` convention from Phase 1. Embedded `data` is proto content and stays unconstrained; the serializer quotes and escapes it during emission.

## `collision_from_image`

`collision_from_image(png_bytes, *, mode="box" | "convex_hull") -> str` is a pure bytes-in/fragment-out function: file I/O belongs to the MCP tool layer. Opaque pixels are those with alpha > `_ALPHA_THRESHOLD` (127).

- **`box`**: `TYPE_BOX` sized from the **inclusive bounding box of opaque pixels** — half-extents `bbox_width/2`, `bbox_height/2`, with a fixed z half-depth of `_BOX_HALF_DEPTH` (10.0). A partially transparent canvas yields the opaque region's bounds, not the canvas's (a test pins a 21×11 opaque region in a 64×64 canvas → half-extents 10.5/5.5).
- **`convex_hull`**: hull of the opaque pixel **centres** (Andrew's monotone chain), flipped to Defold's y-up (`y' = cy - y`), centred on the bounding-box centre, forced counter-clockwise (positive signed area), z = 0.0, reduced to at most `_MAX_HULL_VERTICES` (16) by iteratively dropping the vertex with the smallest perpendicular distance to the line between its neighbours. Degenerate inputs raise `ValueError`: fewer than three opaque pixels, or a hull that collapses below three vertices (collinear pixels).

## Open questions carried from review

- The 16-vertex hull cap follows the plan ("Box2D polygon limit"); stock Box2D `b2_maxPolygonVertices` is 8. Verify against Defold's physics documentation before shipping collision generation (tracked for the server-wiring task).
- Whether the Defold editor accepts **embedded** `.particlefx` components is unverified; the guardrail currently blocks it (conservative bias).

## Where the behaviour is pinned

`tests/test_textproto_roundtrip.py`: `TestGuardrails` (exact gui message, tilemap normalisation, embedded/referenced tree shapes, embedded+referenced round-trip through the serializer) and `TestCollisionFromImage` (partially transparent box bounds, non-square boxes, hull vertex counts, empty-image and collinear rejections).
