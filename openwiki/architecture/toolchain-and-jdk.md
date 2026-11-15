---
type: architecture
title: Toolchain manager and JDK handling
description: Defold version resolution with TTL caching, atomic verified artifact downloads, first-qualifying JDK 25 discovery, and the hardened Adoptium JDK installer.
tags: [defold, toolchain, jdk, downloads, security]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-15T14:41:05.547Z
sources:
  - id: openwiki-source-18a04b4bf25ad2fe02e760b4
    resource: repo://src/defold_mcp/toolchain.py
  - id: openwiki-source-aa918e62d579257ce43a1ddf
    resource: repo://tests/test_deps_sync.py
generated: { by: "opencode", at: "2026-11-15T14:41:05.547Z" }
---

# Toolchain manager and JDK handling

`src/defold_mcp/toolchain.py` owns everything the headless backend needs on disk: a resolved Defold version, `bob.jar`, the engines, the builtins/ref-doc archives — and a Java 25 runtime to run bob, since Defold 1.14.x `bob.jar` is compiled for OpenJDK 25 (class file 69.0) while hosts commonly carry older JDKs (this one has 21 for other work, which the toolchain never disturbs).

## Version resolution — `resolve_version(spec, …)`

- **Channels** (`stable`, `beta`, `alpha`): `GET https://d.defold.com/{channel}/info.json` → `{"version": "1.14.1", "sha1": "ee28…"}`. Any non-200 **raises** (`ValueError` naming the spec, URL and status) — a transient 5xx or removed channel must never silently degrade the toolchain. A 200 missing the `version` key raises naming the URL. **Live finding:** every download host redirects (301/302/307), so the default client follows redirects.
- **Pinned specs** (`1.14.1`) — **live-corrected**: `d.defold.com` serves NO per-version `info.json` (404). Resolution probes the channels in preference order (`stable → beta → alpha`) and adopts the matching channel's sha1, cached under the pin (a two-matching-channels test locks the stable-over-beta order and proves beta is never consulted when stable matches). When NO channel carries the pin, resolution falls back to GitHub-release mode `{"version": spec, "sha1": None}` (only bob.jar and ref-doc have version-keyed URLs — engines cannot resolve in this mode; documented). Failures of any kind are never cached.
- **TTL cache**: successful resolutions land in `<cache>/versions.json` (atomic write) with a 1-hour TTL; `force=True` bypasses. Within the TTL, no re-request occurs (pinned by a request-counting test).

## `check_java()` — first-qualifying discovery

Candidates in order: the portable `<cache>/jdk-25/bin/java`, `JAVA_HOME/bin/java`, then `PATH`. Each existing candidate is **probed** (`java -version`, stderr parsing handles `openjdk version "25.0.1"`, legacy `java version "1.8.0_442"`, and version-less output); the first with major ≥ 25 wins. A *broken* portable JDK cannot shadow a working `JAVA_HOME` Java — the first failure is reported only when nothing qualifies. When the best available is too old, the diagnostic names both escape hatches: `defold_setup_toolchain(install_jdk25=True)` and the Editor `/bob` route (which manages its own Java). Missing Java returns `ok: False` without raising. `java_executable()` returns the same winner as a path (or `None`), so the two functions agree in every case.

## `download_artifact(url, dest, expected_magic=…)`

Atomic (`.tmp` + `os.replace`, parent dirs created) with optional magic-byte verification — `b"PK"` for jars/zips; a mismatch raises `ValueError` naming expected vs actual and leaves neither `.tmp` nor destination behind. Artifacts: `bob.jar` from `https://d.defold.com/archive/{sha1}/bob/bob.jar` (GitHub-release fallback by version tag), `dmengine`/`dmengine_headless` from `.../engine/{platform}/…` (`chmod 0o755` on POSIX), `builtins.zip` and `ref-doc.zip` from `.../engine/share/…`.

## `install_jdk25()` — hardened extraction

Downloads Adoptium (`https://api.adoptium.net/v3/binary/latest/25/ga/{os}/{arch}/jdk/hotspot/normal/eclipse`), extracts the top-level `jdk-25.x.y/` into `<cache>/jdk-25`, and is idempotent (existing `bin/java` short-circuits). Extraction defence-in-depth, mirroring `deps.extract_zip`:

- **validate-all-then-write**: every member is checked before anything is written;
- `_reject_unsafe_members`: absolute paths, `..` components, and **any backslash** in a member name (Windows traversal) are rejected;
- a resolved-path containment backstop (`is_relative_to`) per written member in both zip and tar branches;
- a single-root guard on the zip branch (mirroring the tar branch) so multi-root or root-less archives cannot mis-strip;
- symlink members are skipped (documented — Adoptium layouts are regular files);
- `_check_jdk_layout` raises when the extraction produced no `bin/java`.

`ensure_toolchain(...)` orchestrates resolution + downloads into `<cache>/defold-{version}/` and calls `install_jdk25` when requested and no qualifying Java exists. Artifacts are **reused** when present (live finding: every tool call used to re-fetch ~300 MB), with magic bytes re-verified on reuse — `PK` for jars/zips and `b"\x7fELF"` for Linux engines (`_engine_magic`), riding the corrupt-artifact re-download path. Residual: `download_artifact` buffers whole bodies in memory (~200 MB peak for the JDK); macOS/Windows engines have no cheap magic on reuse.

`read_installations()` parses the OS-correct Defold Editor `installations.json` (Linux `${XDG_STATE_HOME:-~/.local/state}/Defold/…`), tolerating malformed JSON by returning `[]`.

All behaviour is pinned hermetically in `tests/test_deps_sync.py` (shared with the deps suite): TTL request counting, 5xx/404 fallback boundaries, magic-check atomicity, JDK parsing edge cases, portable-first and broken-portable fallbacks, and the extraction hardening tests.
