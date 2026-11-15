# Cross-platform targets and genre recipes

Defold bundles every target from one project with `bob.jar` — no Android Studio,
no Xcode for the common cases. The differences that bite are per target and are
mostly settled by `game.project` and a bundle flag.

## Target matrix

| Target | `defold_bundle(platform=...)` | Default architectures | What breaks |
|---|---|---|---|
| HTML5 | `wasm-web` | `wasm-web` (opt in `wasm_pthread-web` for threads) | Runs **PUC Lua 5.1.4**, not LuaJIT. Audio is blocked until the first input event. Keep the heap modest and the download small. |
| Android | `armv7-android` | `armv7-android`, `arm64-android` (add `x86_64-android` for emulators) | Needs the safe-area and cutout settings, and R8 shrinking needs the builtins keep rules. |
| iOS | `arm64-ios`, `arm64_sim-ios` | `arm64-ios` | LuaJIT runs in **interpreter mode** (no JIT). Device builds need `--identity` and `--mobileprovisioning`; the simulator target needs neither. Localisations must be declared for `sys.get_info().language`. |
| Linux | `x86_64-linux`, `arm64-linux` | matches `--platform` | Nothing; this is the zero-config desktop and handheld target. |
| Windows | `x86_64-win32` | `x86_64-win32` | Uses `.ico` for `[windows] app_icon`. |
| macOS | `arm64-macos`, `x86_64-macos` | matches `--platform` | Metal is the default backend; `/profiler/gputrace` needs `METAL_CAPTURE_ENABLED=1`. |
| Consoles | licensed SDKs | vendor | Gated behind Nintendo/Sony/Microsoft developer status, and the toolchain is not public. Plan for a separate build host. |

Store and platform rules that are not optional: iOS rejects apps that download
executable code, so no runtime Lua/JIT tricks; Android wants 64-bit for Play;
HTML5 has no threads unless you opted into them, and no file system beyond
`sys.save`/`sys.load`.

## 2D pixel art

```ini
[display]
width = 960
height = 540

[physics]
scale = 0.01
gravity_y = -1000.0
velocity_threshold = 100.0
use_fixed_timestep = 1

[engine]
fixed_update_frequency = 60
```

* `defold_project_init(profile="2d_pixel")` writes exactly this shape.
* Nearest-neighbour filtering is a texture-profile setting, not a project
  setting: create a `.texture_profiles` entry with `filter_mag = nearest` and
  `filter_min = nearest`, and point sprites at it. Crisp pixels also need
  integer zoom and `subpixels = 0` on the sprite.
* Keep the camera at integer positions; a half-pixel offset blurs the whole
  scene.
* Physics at `scale = 0.01` turns a 100 px sprite into a 1 m body, inside
  Box2D's comfortable 0.1–10 m range. With `scale = 1.0` the same sprite is a
  100 m skyscraper and every collision is unstable.

## 2D HD for mobile

```ini
[display]
width = 1280
height = 720

[gui]
safe_area_mode = both
```

* `gui.safe_area_mode` keeps GUI nodes out of notches and rounded corners; in
  code, `window.get_safe_area()` returns the usable rectangle in pixels.
* Android: `[android] immersive_mode = 1`, `display_cutout = 1`, and
  `r8_keep_rules = /builtins/manifests/android/dmengine.keep` before enabling R8.
* iOS: `[ios] localizations = en, de, ...` — `sys.get_info().language` returns
  nothing useful without it.
* Bundle a universal Android artifact with
  `defold_bundle(platform="armv7-android", bundle_format="apk,aab", architectures="armv7-android,arm64-android")`.

## 3D

```ini
[physics]
type = 3D
scale = 1.0
```

* `defold_project_init(profile="3d_pbr")` sets this and mounts a perspective
  camera.
* Import `.glb`/`.gltf` with the built-in glTF materials
  (`gltf_instanced.material` for repeated static props,
  `gltf_skinned_instanced.material` for animated crowds). See
  `shaders-and-materials.md` for the PBR sampler contract.
* Bullet3D (`bullet3d.world`, `bullet3d.rigid_body`, `bullet3d.constraint`) is the
  C++ 3D physics route; Box2D v3 (`b2d.*`) is the 2D one. Do not mix them in one
  component.
* 3D on mobile is fill-rate bound: prefer baked lighting, disable shadows on the
  lowest tier, and check the bundle's asset breakdown.

## HTML5 specifics

* **Lua 5.1 only.** No `goto`, no `//`, no `&`/`|`/`<<`, no `_ENV`. Use `bit.*`.
  Code that runs on the desktop will corrupt arithmetic or fail to load here.
* **Audio needs a gesture.** Browsers block autoplay; start music and effects
  from the first `on_input` callback.
* **Settings that shape the page.** Only *parse-ability* is proven: bob accepts
  the `[html5]` section below against Defold 1.14.1, and the project template
  ships it. That the engine honours these keys *inside* `[html5]` has **not**
  been verified live — stock Defold reads `scale_mode` from `[display]`, and
  `dmengine_headless` shows no `html5.*` key lookups. The `[html5]` placement
  follows the implementation plan's §1.3 table:

  ```ini
  [html5]
  scale_mode = Stretch
  heap_size = 64
  retry_count = 1000
  ```

  If a live check contradicts this, treat the block as `[display] scale_mode`
  plus whatever `[html5]` keys the engine actually reports.
* Verify the payload: a Defold HTML5 build is a few MB, so an unexpectedly large
  `game.arcd` means an unreferenced asset slipped into `custom_resources`.

## Extensions that are worth the dependency

Native extensions compile through Defold's cloud Extender during `bob build`, so
no local NDK or Xcode is needed. Declare them with
`defold_sync_deps(action="add", url=...)`.

* **Ads and monetisation:** `extension-admob`, `extension-applovin` (MAX),
  `extension-levelplay` (Unity LevelPlay), `extension-adinfo` (IDFA/AAID and
  ATT), `extension-adpf` (Android thermal and performance signals),
  `extension-review` (in-app rating), `extension-iap` (in-app purchases).
* **Web portals:** `extension-poki-sdk`, `extension-crazygames`,
  `extension-playables` (YouTube Playables), `extension-fbinstant`. Each portal
  wants its own SDK initialisation and pause/resume handling.
* **Desktop:** `extension-steam` (Steamworks).

## Bundle commands

The MCP tool is the short path; the equivalent `bob` invocations are useful in
CI. bob takes commands first, then options, and must run with the project
directory as its working directory:

```bash
# HTML5, release
java -jar bob.jar build bundle --platform wasm-web --variant release --archive

# Linux desktop, debug
java -jar bob.jar build bundle --platform x86_64-linux --variant debug --archive

# Android, both APK and AAB, two architectures
java -jar bob.jar build bundle --platform armv7-android --variant release \
    --bundle-format apk,aab --architectures armv7-android,arm64-android --archive

# iOS simulator (no provisioning profile needed)
java -jar bob.jar build bundle --platform arm64_sim-ios --variant debug --archive
```

Useful flags: `--build-report-json <path>` for a size breakdown,
`--settings <file>` to override `game.project` values for one build (this is how
`defold_test_run` redirects the bootstrap collection without touching your
project), and `--resource-cache-local <dir>` to reuse compiled artifacts.

Every package ends up under `build/`; `defold_bundle` returns that listing with
sizes, which is the cheapest way to catch an empty `.wasm` or a missing
`index.html` before shipping.
