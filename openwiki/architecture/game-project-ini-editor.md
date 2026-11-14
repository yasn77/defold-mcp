---
type: architecture
title: game.project INI editor
description: How defold-mcp parses and edits Defold's game.project losslessly — the line-list model, compiled-path 'c' suffix enforcement, dependencies#N handling, injection guards and atomic saving.
tags: [defold, game-project, ini, parser, mutation]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-14T16:49:47.288Z
sources:
  - id: openwiki-source-e97c31ef8fcfbd50708b634f
    resource: repo://src/defold_mcp/config.py
  - id: openwiki-source-d57d093dceb0da08744e2de5
    resource: repo://src/defold_mcp/project.py
  - id: openwiki-source-d0ba0764c79275c3c198800b
    resource: repo://tests/test_project_ini.py
generated: { by: "opencode", at: "2026-11-14T16:49:47.288Z" }
---

# game.project INI editor

`src/defold_mcp/project.py` owns the reading and editing of Defold's `game.project` file. It is the single sanctioned write path for project settings: MCP tools that modify settings go through `GameProject`, never through raw text patching. Its contract is **losslessness** — comments, blank lines, key order, section order and the file's newline convention all survive every edit unchanged.

## Why not `configparser`

Python's stdlib `configparser` is unusable for `game.project` for three reasons: it lowercases key names arbitrarily (corrupting Defold's case-sensitive keys), it cannot represent keys containing `#` (the modern indexed dependency keys are literally `dependencies#0`, `dependencies#1`…), and its read-modify-write cycle strips comments and blank lines. The editor therefore implements a dedicated ~250-line line-preserving model.

## Parsing model: an ordered line list

`GameProject.__init__` loads the file with `open(newline="")` — disabling universal-newline conversion so CRLF-authored files are loaded byte-faithfully — and keeps the content as `self._lines` from `text.splitlines()`. Two facts drive the output path:

- `splitlines()` consumes the newline characters, so the detected convention (`self._newline`, `\r\n` or `\n`) is recorded at load and used to join lines in the `text` property and to append the original trailing newline.
- Every accessor and mutator recomputes a fresh index via `_index()`: section headers (regex `^\[(.+)\]\s*$`) in file order, plus a `(section, key) → line index` map. Recomputing per operation eliminates any stale-index bug class after insertions and deletions.

Lines that are blank, start with `#`, or contain no `=` are skipped by the indexer but preserved in the list. Keys are extracted with `line.partition("=")[0].strip()` — the split is on the **first** `=`, so values containing `=` round-trip intact.

## Project root resolution

`resolve_project_root()` locates the project root:

1. If `DEFOLD_PROJECT_ROOT` is set, it is the root and must contain `game.project` — otherwise `FileNotFoundError`.
2. Otherwise it walks upward from the start directory (or the cwd) through at most **eight parents** looking for a directory containing `game.project`, raising `FileNotFoundError` when none is found.

## Compiled-path `c` suffix enforcement

Defold stores six settings as *compiled* artifact paths, each with a trailing `c` (`main_collection = /main/main.collectionc`, not the on-disk `.collection`). `set()` matches `f"{section}.{key}"` against the imported `COMPILED_SETTING_KEYS` tuple from `defold_mcp.config` and auto-appends `c` when the value does not already end with it — making the operation idempotent (never `…cc`). The six keys: `bootstrap.main_collection`, `bootstrap.render`, `bootstrap.debug_init_script`, `input.game_binding`, `input.gamepads`, `display.display_profiles`. All other keys are written verbatim.

Matching is section-qualified because the bare key names (`render`, `width`) also appear in unrelated sections.

## Dependency management

Modern Defold writes dependencies as indexed keys under `[project]`; older projects used one comma-separated `dependencies = url0, url1` line. The editor handles both:

- **Reading** (`get_dependencies()`): collects `dependencies#N` keys and sorts them by the *number* after `#` (`#2` sorts before `#10`); the legacy comma-separated key is consulted only when no indexed keys exist. Duplicates are never silently removed — what is on disk is reported.
- **Writing** (`add_dependency` / `remove_dependency`): both funnel into `_rewrite_dependencies(urls)`, which replaces the entire dependency block — indexed keys and any legacy line — with clean, gapless `dependencies#0..#N-1` keys. Existing dependency lines are deleted in reverse index order and the replacement block is spliced in at the first old position; when no dependency lines exist, the block is inserted after the last `[project]` member (skipping any trailing blank run so the separator stays visually attached to the section), or appended with a new `[project]` header at end-of-file. Comments, blank lines and all other sections are untouched. `add_dependency` is a no-op returning `False` when the URL is already present; `remove_dependency` removes the first match, `False` when absent.

## `include_dirs` splitting

`[library] include_dirs` is officially space-separated, but community libraries sometimes use commas. `parse_include_dirs()` splits on runs of whitespace and/or commas (`re.split(r"[\s,]+", raw.strip())`, empty parts filtered), so both conventions parse.

## Injection guards

Because future phases feed LLM-supplied arguments into these methods, every public mutator validates before mutating:

- `set()` rejects any `\n` or `\r` inside section, key or value, and `]` inside a section name;
- `add_dependency()` / `remove_dependency()` reject `\n`/`\r` in the URL.

A multi-line value could otherwise inject forged lines (e.g. a whole new `[project]` section with an attacker-chosen dependency URL) — a dependency supply-chain vector.

## Saving

`save()` writes atomically: the content goes to `<name>.tmp` beside the target, then `os.replace()` swaps it in. The write uses `newline=""` so no `os.linesep` translation occurs on Windows (a CRLF file would otherwise be re-expanded into `\r\r\n`). No fsync is performed and there is no file locking — acceptable for a single-user local tool, but a crash mid-write can orphan a `.tmp` file.

## Known limitation

Keys containing `=` are not validated by the guards (the parse itself splits on the first `=`). The MCP tool boundary must police this when argument validation lands in the server-wiring phase.

## Where the behaviour is pinned

`tests/test_project_ini.py` (24 tests total in the phase suite together with `tests/test_config.py`) pins every rule above: line preservation, the blank-line insertion position, numeric `#N` ordering, legacy rewrites, indexed-beats-legacy precedence, idempotent `c` suffixing, byte-level CRLF round-trips (`read_bytes()` on the assertion side), all guard rejections, the 8-parent walk cap, `get()` defaults, `delete`/`delete_section`, and end-of-file insertion.
