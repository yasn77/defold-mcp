---
type: architecture
title: Templates and the mutations registry
description: The four project scaffolding profiles with the Defold licence attribution module, and the 14-op textproto mutation registry feeding defold_mutate_resource.
tags: [defold, templates, scaffolding, mutations, registry]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T13:47:50.676Z
sources:
  - id: openwiki-source-2c7c1fca6695097aaa0e289a
    resource: repo://src/defold_mcp/templates/__init__.py
  - id: openwiki-source-ce941463b68b754d5210fcd3
    resource: repo://src/defold_mcp/textproto/mutations.py
  - id: openwiki-source-edd5cc407110cd57fc145906
    resource: repo://src/defold_mcp/textproto/serializer.py
  - id: openwiki-source-6adafae9b5e5047999e93399
    resource: repo://tests/test_templates.py
  - id: openwiki-source-c7f35c27a3e804b5af2c8277
    resource: repo://tests/test_textproto_roundtrip.py
generated: { by: "opencode", at: "2026-11-15T13:47:50.676Z" }
---

# Templates and the mutations registry

`src/defold_mcp/templates/__init__.py` scaffolds new Defold projects; `textproto/mutations.py` gained the plan's full operation registry in the server-wiring phase. Together they are what `defold_project_init` and `defold_mutate_resource` serve.

## The four profiles

`render_project(dest_root, *, title, profile, dependencies=None, with_deftest=False)` writes a minimal valid Defold 1.14.1 project for `2d_pixel`, `2d_hd`, `3d_pbr` or `minimal`:

- **`game.project`** with the plan-pinned defaults: 2D profiles carry `[physics] scale = 0.01`, `gravity_y = -1000.0`, `velocity_threshold = 100.0`, `use_fixed_timestep = 1` plus `[engine] fixed_update_frequency = 60` (the plan's §1.5 point 10 names `[engine]` explicitly); 3d_pbr carries `type = 3D`, `scale = 1.0`; all profiles carry `[html5] scale_mode = Stretch`, `heap_size = 64`, `retry_count = 1000`; bootstrap references the builtins render and debug scripts (`/builtins/render/default.renderc`, `/builtins/scripts/debug_init.scriptc`).
- **`main/main.collection`** built through `serialize_textproto` from the same tree shape parsing produces — so the rendered file round-trips byte-exactly. 2D profiles embed a `hero` game object with an embedded sprite (material `/builtins/materials/sprite.material`) and a referenced script; 3d_pbr embeds a camera; **all four profiles mount the referenced `main.script`** so the licence notice prints at runtime.
- **`main/main.script`** declares a literal `go.property("speed", 10)` (Defold's compile-time parser requires literals), prints the title (Lua-escaped: a title containing quotes renders safely) and requires the notice module.
- **`license/defold_notice.lua`** — the Defold Foundation attribution module satisfying the Defold License's clause 4(b) notice requirement, shipped out of the box.
- `.gitignore` / `.defignore` for `.deps`, `build`, `.internal`.
- `KNOWN_LIBRARY_URLS` maps `deftest`, `monarch`, `druid`, `automation_bridge` to their GitHub archives — pointed at moving `master` branches with a documented obligation to pin release tags in the live phase. The optional `with_deftest` flag scaffolds the test runner.

## The 14-op registry

`apply_operations(tree, operations, *, extension=None)` — registry dispatch (`ValueError` naming the offending op index for unknown ops), deep-copied input, new tree out, every string argument passing the single-line guards, and the result normalised through `decode_embedded_data` so freshly built resources serialise byte-exactly on first write:

`create`, `add_instance` (the real Defold shape: `prototype` + math fields), `add_embedded_instance`, `add_subcollection`, `add_component` (delegating to the reviewed direct function with its guardrails), `set_property`, `remove_child`, `atlas_add_images`, `atlas_set_animation`, `tilesource_configure`, `tilemap_set_tiles` (assumed tree shape documented; corrected against a real `.tilemap` live), `gui_add_node`, `gui_set_layout_property`, `collision_add_shape` (inline shape dict or `image_png_base64` → the reviewed `collision_from_image` generator).

The serializer's `extension=` parameter (canonical `SCHEMA_BY_EXTENSION` top-level ordering, opt-in, default preserving insertion-order byte-exactness) is the last piece of the Phase-2 schemas obligation, closed here.

## Where the behaviour is pinned

21 template tests (per-profile physics keys, byte-exact collections, Lua escaping, licence mounting, dependency wiring) and 29+ registry tests in `tests/test_textproto_roundtrip.py` Part C — one per op, the guards, and the end-to-end create → add_component → add_instance → serialize → re-parse byte-exact chain.
