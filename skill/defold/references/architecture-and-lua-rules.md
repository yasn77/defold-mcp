# Architecture and Lua rules

Defold's runtime is a data-oriented C++ engine with a Lua layer on top. This guide
covers the addressing model, the four script contexts, and the Lua patterns that
break when you write them the way you would in another engine.

## Addresses

A runtime address has the form `[socket:][path][#fragment]`:

| Part | Meaning | Example |
|---|---|---|
| socket | A separate game world loaded through a collection proxy | `level:` |
| path | The slash-prefixed path of a game object, from the world root | `/player` |
| fragment | A component id inside that game object | `#controller` |

```lua
msg.post("/player#controller", "jump")      -- object + component
msg.post("level:/enemy#script", "wake_up")  -- across a proxy-loaded world
msg.post(".", "acquire_input_focus")        -- "." is this game object
msg.post("#", "reset")                      -- "#" is this component
```

Collections are a **build-time** construct. `main.collection` containing an
instance `hero` of `hero.go` becomes the address `/hero`, and a collection
instance `level` of `level.collection` containing `enemy` becomes
`/level/enemy`. A collection is not addressable at runtime, so `/level` alone
will fail.

`msg.post("child", "set_parent", { parent_id = go.get_id("parent") })` changes
the **transform hierarchy only**. The address never changes: it was fixed at
spawn.

## Relative strings versus hashed ids

Inside a world, a relative string is resolved against that world's hash state:

```lua
go.set_position(vmath.vector3(10, 20, 0), "hero")  -- works: relative string
go.set_position(vmath.vector3(10, 20, 0), hash("/hero"))  -- works: absolute id
go.set_position(vmath.vector3(10, 20, 0), hash("hero"))   -- fails in a sub-collection
```

`hash("hero")` carries no collection context, so from inside `/level` it cannot
resolve. Prefer the relative string, or an absolute `hash("/full/path")`.
`add_instance`-created ids are equally hashable.

Never depend on `tostring(hash)` for logic: in release builds the reverse lookup
table is stripped and you get `hash: [1234567890 (unknown)]`, and `print()` is
silenced in release builds, so debugging output must not be the mechanism either.

## The four script contexts

| Context | Extension | Has | Does not have |
|---|---|---|---|
| Game object | `.script` | `go.*`, component APIs (`sprite.*`, `factory.*`, `physics.*`, `sound.*`, `tilemap.*`), `go.property()` | `gui.*`, `render.*` |
| GUI | `.gui_script` | `gui.*`, `gui.get(node, prop)` / `gui.set(node, prop, value)` for node properties | `go.*`, `render.*`, `go.property()`, `fixed_update`, `late_update` |
| Render | `.render_script` | `render.*`, `graphics.*`, `camera.*` | `go.*`, `gui.*` |
| Editor | `.editor_script` | `editor.*` in the Editor JVM (Lua 5.2) | anything from the running game |

Render scripts have two signature quirks: `update(self, dt)` receives **no**
delta time, and `on_message(self, message_id, message)` receives **no** sender.

Editor scripts run in the Editor process, not the engine, and `editor.transact()`,
`editor.execute()`, `editor.create_resources()` and `editor.save()` are
*long-running*: calling them at top level, or from a menu item's `active()`
callback, raises `Cannot use long-running editor function in immediate context`.
Call them from `execute()` handlers. Defold's HTTP API already exposes the
structured transactions you need, which is why this skill never asks you to write
raw editor scripts.

## `self`, and the trap of shared state

`self` is C userdata. `pairs(self)`, `ipairs(self)` and `pprint(self)` all fail —
access fields directly.

With the default `[script] shared_state = 1`, the game objects, GUI scripts and
render script share **one** Lua VM. A module-level local is therefore shared by
every instance of that script:

```lua
local hit_points = 100          -- shared by ALL enemies: a bug

function init(self)
    self.hit_points = 100       -- per instance: correct
end
```

Set `[script] shared_state = 0` only when you deliberately want strict isolation
per script *type*; it costs one VM per type and does not change the rule above.
Delete `update`/`fixed_update` callbacks you do not implement — each one costs a
C-to-Lua call per instance per frame.

## `go.property()` is parsed before Lua runs

Bob and the Editor parse `go.property()` statically, so the value must be a
literal:

```lua
go.property("speed", 10)                          -- ok
go.property("alive", true)                        -- ok
go.property("label", "hero")                      -- ok
go.property("atlas", resource.atlas("/main/main.atlas"))  -- ok
go.property("url", msg.url())                     -- ok
go.property("half", 50 / 2)                       -- fails to compile
```

Overrides from a collection instance or `go.set`/`go.get` work as expected at
runtime; only the default expression is restricted.

## Messages

```lua
msg.post(target, message_id, [message_table])
```

* The message table is limited to **2 KB**. Pass ids, not payloads.
* Functions and userdata cannot cross the boundary.
* The dispatcher runs at most **75 passes** per frame, so a message chain deeper
  than that continues on the next frame rather than deadlocking.
* `on_message(self, message_id, message, sender)` receives hashed `message_id`
  and a `url` sender; compare with `hash("jump")`.

Prefer messages over polling, and `go.animate(..., go.EASING_*, duration, delay,
complete_fn)` or `timer.delay(delay, false, callback)` over counting frames in
`update`.

## Modules

```lua
local utils = require("main.utils")   -- /main/utils.lua
```

Rules: dot-separated, project-relative, no leading slash and no `.lua` suffix.
A common shape:

```lua
-- /main/utils.lua
local M = {}

function M.clamp(value, low, high)
    if value < low then return low end
    if value > high then return high end
    return value
end

return M
```

Hot reload replaces the module table. Callers that captured the old table with
`local utils = require(...)` keep the stale one until `package.loaded["main.utils"]`
is cleared and the dependants reload too. For hot-reload-friendly state, keep
data on `self` and functions in modules.

## Reachability

`bob` builds exactly the graph reachable from `game.project`: the bootstrap
collection, its referenced prototypes and collection instances, the render
script, and anything referenced from those. Unreferenced files do not enter the
archive, so `sys.load_resource("/data/levels.json")` returns nothing unless you
declare:

```ini
[project]
custom_resources = /data
```

The same rule catches test collections: a test collection that nothing instances
is never compiled, which is why `defold_test_run` builds it as the entry point
itself.

## Library idioms

Check the Asset Portal first (`defold_docs_search(scope="assets")`) and read the
library's own docs for exact signatures; the shapes below are the idioms that
matter for architecture decisions.

* **Monarch** — screen manager. A screen declares an id and a view
  (`gui` or `collection`), and navigation is `monarch.show(hash("playing"))`.
  Use it instead of hand-rolled scene switching, and load heavy screens lazily.
* **Druid** — GUI component kit. Instantiate once per `.gui_script`
  (`local druid = require("druid.druid")`, then `druid.new(self)` in `init`),
  forward `update(dt)` and input to it, and build buttons, lists and progress
  bars from components rather than from raw nodes.
* **DefTest** — test runner for `defold_test_run`. A test collection mounts one
  runner script that calls `deftest.log()`, and the suite's results appear as
  `TEST {...}` lines that the MCP server parses.

Keep third-party libraries under `.deps/` by declaring them with
`defold_sync_deps(action="add", url=...)`, never by copying files into the
project: `.deps/` is the directory `bob` resolves library paths from, and
`/builtins` only becomes greppable once dependencies are unpacked.
