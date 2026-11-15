# Proto schemas quick reference

`defold_mutate_resource(resource_path, operations)` applies a **batch** atomically:
every operation runs against a copy of the parsed tree and the file is written
once, so a bad operation leaves the resource untouched. The reason this tool
exists is escaping: nested Protobuf Text inside a `.collection` needs `\\\"` for
inner quotes, except on the closing line, and one wrong backslash makes the
Editor refuse the file. Never hand-write those strings.

## The fourteen operations

Every operation carries `"op"`. Unlisted keys are ignored.

### `create`

Start a fresh resource. Only valid when the tree is empty.

```json
{"op": "create", "resource_type": "go"}
```

`resource_type` defaults to the file extension (`main.collection` → `collection`);
it also seeds the empty repeated fields for a `.go`.

### `add_instance`

Add a prototype instance to a collection. `position`, `rotation` and `scale`
are optional objects.

```json
{"op": "add_instance", "id": "hero", "prototype": "/main/hero.go",
 "position": {"x": 640.0, "y": 360.0}}
```

### `add_embedded_instance`

Inline a game object. Each component is either **referenced** (`component`, the
normal case for scripts and `.gui`) or **embedded** (`type` plus `data`).

```json
{"op": "add_embedded_instance", "id": "camera",
 "components": [{"id": "script", "component": "/main/main.script"}],
 "position": {"x": 0.0, "y": 0.0}}
```

### `add_subcollection`

Instance another collection. `collection` is the **source** path.

```json
{"op": "add_subcollection", "id": "level", "collection": "/levels/level1.collection"}
```

### `add_component`

Attach a component to an existing game object or collection.

```json
{"op": "add_component", "id": "sprite", "type": "sprite", "embedded": true,
 "data": "default_animation: \"idle\"\nmaterial: \"/builtins/materials/sprite.material\"\n"}
```

```json
{"op": "add_component", "id": "script", "component": "/main/hero.script"}
```

### `set_property`

Set one scalar field. `parent` is a dotted path into the tree, `""` is the root,
and numeric segments address list entries.

```json
{"op": "set_property", "parent": "", "key": "name", "value": "main"}
```

```json
{"op": "set_property", "parent": "embedded_instances.0", "key": "id", "value": "hero"}
```

### `remove_child`

Delete the first list entry matching a field value.

```json
{"op": "remove_child", "parent": "instances", "match": {"key": "id", "value": "enemy"}}
```

### `atlas_add_images`

```json
{"op": "atlas_add_images", "images": ["/main/hero.png", "/main/tiles.png"]}
```

### `atlas_set_animation`

```json
{"op": "atlas_set_animation",
 "animation": {"id": "run", "playback": "PLAYBACK_LOOP_FORWARD", "fps": 12,
               "images": ["/main/run_1.png", "/main/run_2.png"]}}
```

### `tilesource_configure`

Set arbitrary top-level `.tilesource` fields.

```json
{"op": "tilesource_configure",
 "keys": {"tile_width": 16, "tile_height": 16, "image": "/main/tiles.png"}}
```

### `tilemap_set_tiles`

Write a layer's grid. `layer` is an index or a layer id; `rows` are lists of
comma-separated cells.

```json
{"op": "tilemap_set_tiles", "layer": "ground",
 "rows": [["1,1,1,1"], ["1,0,0,1"], ["1,1,1,1"]]}
```

### `gui_add_node`

```json
{"op": "gui_add_node",
 "node": {"id": "score", "position": {"x": 20.0, "y": 700.0},
          "size": {"x": 200.0, "y": 40.0}, "text": "0"}}
```

### `gui_set_layout_property`

Create the layout and node if they do not exist, then set one property.

```json
{"op": "gui_set_layout_property", "layout_id": "Landscape",
 "node_id": "score", "key": "x", "value": 30.0}
```

### `collision_add_shape`

Either declare a shape, or derive one from a PNG's alpha channel.

```json
{"op": "collision_add_shape",
 "shape": {"shape_type": "TYPE_BOX", "position": {"x": 0.0, "y": 0.0}}}
```

```json
{"op": "collision_add_shape", "image_png_base64": "<base64 PNG>", "mode": "convex_hull"}
```

`mode` is `box` or `convex_hull`; a convex hull is capped at 16 vertices.

## Guardrails

* **`.gui` cannot be embedded.** `gui`, `script` and `particlefx` are
  non-embeddable: reference them with `component`, never `type` + `data`. An
  attempted inline `.gui` is rejected rather than silently written.
* **An inline tilemap is written as `tilegrid`.** `add_component` with
  `type: "tilemap"` is normalised to `type: "tilegrid"`, which is what Defold's
  proto expects for an inline tilemap.
* **Collection instances take SOURCE paths.** `collection_instances.collection`
  is `/levels/level1.collection`, never `.collectionc` — bob resolves it with
  `project.getResource` and has no reverse mapping, so the `c` form fails the
  build with "the collection … can't be found".
* **Math defaults are omitted.** An all-default `position`/`rotation`/`scale`
  block is dropped, so a freshly written file matches what the Editor writes and
  round-trips byte-exactly. A partial block (a 2D position with only `x` and `y`)
  is kept: the missing component is meaningful.
* **Paths are contained.** `resource_path` resolves inside the project root;
  `..`, normalised escapes and symlink escapes are rejected.
* **Editor versus headless dialect.** When a Defold Editor is open *and* the batch
  uses the Editor's own dialect (`add_resource` with `path`/`template`, or
  `set_property` with `node`), the batch is sent as an `editor.transact()`. Every
  other payload goes through the deterministic serialiser. Responses report
  which path served them as `backend`.

## `game.project` rules

Use `defold_project_settings(action="set", section=..., key=..., value=...)`
rather than editing the file: it preserves comments, formatting and key order,
and it appends the compiled-path suffix for you.

Six keys name **compiled** artifacts and must end in `c`:

| Key | Value shape |
|---|---|
| `bootstrap.main_collection` | `/main/main.collectionc` |
| `bootstrap.render` | `/builtins/render/default.renderc` |
| `bootstrap.debug_init_script` | `/main/debug.scriptc` |
| `input.game_binding` | `/builtins/input/all.input_bindingc` |
| `input.gamepads` | `/builtins/input/default.gamepadsc` |
| `display.display_profiles` | `/builtins/render/default.display_profilesc` |

bob strips the final character to find the source file, so `/main/main.collection`
(no `c`) fails, and naming a resource that does not exist — such as a
`debug_init_script` the engine does not ship — also fails.

Two parsing rules were verified against Defold 1.14.1 and are easy to violate:

* **No comment lines.** bob accepts only section headers, `key = value` pairs and
  blank lines. A `;` or `#` comment makes it report "Could not parse" and abort.
* **Dependencies are indexed.** Write `dependencies#0`, `dependencies#1`, …;
  older projects may still hold one comma-separated `dependencies` value, and
  both shapes are read. `[library] include_dirs` is space-separated by
  specification but is split on `[\s,]+` because community libraries use commas.

Project roots resolve from `DEFOLD_PROJECT_ROOT` first, otherwise by walking up to
eight parents looking for `game.project`.
