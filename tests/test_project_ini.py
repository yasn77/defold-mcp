"""Unit tests for the line-preserving game.project editor."""

import pytest

from defold_mcp.project import (
    GameProject,
    parse_include_dirs,
    resolve_project_root,
)

URL0 = "https://github.com/britzl/monarch/archive/refs/tags/v1.0.zip"
URL1 = "https://github.com/Insality/druid/archive/refs/tags/v1.0.zip"

FIXTURE = """[project]
title = My Game
version = 0.1
# A comment that must survive edits
dependencies#0 = {url0}
dependencies#1 = {url1}

[bootstrap]
main_collection = /main/main.collectionc
render = /render/default.renderc

[library]
include_dirs = monarch utils

[display]
width = 960
""".format(url0=URL0, url1=URL1)


def write_fixture(tmp_path):
    path = tmp_path / "game.project"
    path.write_text(FIXTURE, encoding="utf-8")
    return path


def test_resolve_project_root_walks_up(tmp_path):
    (tmp_path / "game.project").write_text("[project]\n", encoding="utf-8")
    deep = tmp_path / "sub" / "deeper"
    deep.mkdir(parents=True)
    assert resolve_project_root(deep) == tmp_path


def test_resolve_project_root_env_var(tmp_path, monkeypatch):
    (tmp_path / "game.project").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("DEFOLD_PROJECT_ROOT", str(tmp_path))
    assert resolve_project_root() == tmp_path

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("DEFOLD_PROJECT_ROOT", str(empty))
    with pytest.raises(FileNotFoundError):
        resolve_project_root()


def test_set_appends_c_and_idempotent(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    gp.set("bootstrap", "main_collection", "/main/main.collection")
    assert gp.get("bootstrap", "main_collection") == "/main/main.collectionc"
    gp.set("bootstrap", "main_collection", "/main/main.collectionc")
    assert gp.get("bootstrap", "main_collection") == "/main/main.collectionc"
    gp.set("display", "width", "1280")
    assert gp.get("display", "width") == "1280"


def test_get_dependencies_indexed_and_legacy(tmp_path):
    indexed = tmp_path / "indexed.project"
    indexed.write_text(
        "[project]\n"
        "dependencies#0 = url0\n"
        "dependencies#2 = url2\n"
        "dependencies#10 = url10\n",
        encoding="utf-8",
    )
    assert GameProject(indexed).get_dependencies() == ["url0", "url2", "url10"]

    legacy = tmp_path / "legacy.project"
    legacy.write_text("[project]\ndependencies = url0, url1\n", encoding="utf-8")
    assert GameProject(legacy).get_dependencies() == ["url0", "url1"]


def test_add_remove_dependency_sequential(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    url_new = "https://example.com/new.zip"
    assert gp.add_dependency(url_new) is True

    lines = gp.text.splitlines()
    # Unrelated lines survive verbatim: the comment and the blank line
    # between [project] and [bootstrap].
    assert "# A comment that must survive edits" in lines
    i_proj = lines.index("[project]")
    i_boot = lines.index("[bootstrap]")
    assert "" in lines[i_proj:i_boot]

    assert gp.get_dependencies() == [URL0, URL1, url_new]
    for i in range(3):
        assert any(line.startswith(f"dependencies#{i} = ") for line in lines)
    assert not any(
        line.strip().startswith("dependencies =") for line in lines
    )

    # Adding an existing URL is a no-op.
    assert gp.add_dependency(url_new) is False
    assert gp.get_dependencies() == [URL0, URL1, url_new]

    # Removal renumbers the block.
    assert gp.remove_dependency(URL0) is True
    assert gp.get_dependencies() == [URL1, url_new]
    assert gp.remove_dependency("https://absent.example/none.zip") is False


def test_parse_include_dirs(tmp_path):
    assert parse_include_dirs("monarch, utils  druid") == [
        "monarch",
        "utils",
        "druid",
    ]
    gp = GameProject(write_fixture(tmp_path))
    assert gp.get_include_dirs() == ["monarch", "utils"]


def test_save_preserves_comments_and_format(tmp_path):
    path = write_fixture(tmp_path)
    gp = GameProject(path)
    gp.set("project", "version", "0.2")
    gp.save()
    saved = path.read_text(encoding="utf-8")
    assert "# A comment that must survive edits" in saved
    assert "\n\n[bootstrap]" in saved
    assert saved.endswith("\n")


def test_set_creates_missing_section(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    gp.set("physics", "scale", "0.01")
    assert "physics" in gp.sections()
    assert gp.get("physics", "scale") == "0.01"
    lines = gp.text.splitlines()
    assert lines[-1] == "scale = 0.01"
    assert lines[-2] == "[physics]"


def test_get_dependencies_indexed_beats_legacy(tmp_path):
    path = tmp_path / "mixed.project"
    path.write_text(
        "[project]\n"
        "dependencies = url-legacy\n"
        "dependencies#0 = url0\n",
        encoding="utf-8",
    )
    assert GameProject(path).get_dependencies() == ["url0"]


def test_add_dependency_rewrites_legacy_only_file(tmp_path):
    path = tmp_path / "legacy.project"
    path.write_text(
        "[project]\n"
        "title = Legacy\n"
        "dependencies = url0, url1\n",
        encoding="utf-8",
    )
    gp = GameProject(path)
    assert gp.add_dependency("url2") is True
    assert gp.get_dependencies() == ["url0", "url1", "url2"]
    lines = gp.text.splitlines()
    assert not any(line.strip().startswith("dependencies =") for line in lines)
    assert "dependencies#0 = url0" in lines
    assert "dependencies#1 = url1" in lines
    assert "dependencies#2 = url2" in lines


def test_set_new_key_lands_before_trailing_blanks(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    gp.set("bootstrap", "new_key", "value")
    lines = gp.text.splitlines()
    i_render = lines.index("render = /render/default.renderc")
    assert lines[i_render + 1] == "new_key = value"
    assert lines[i_render + 2] == ""
    assert lines[i_render + 3] == "[library]"


def test_value_with_equals_round_trips(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    gp.set("project", "title", "a=b c=d")
    assert gp.get("project", "title") == "a=b c=d"


def test_resolve_project_root_walk_cap(tmp_path):
    (tmp_path / "game.project").write_text("[project]\n", encoding="utf-8")
    deep = tmp_path
    for name in ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9"):
        deep = deep / name
    deep.mkdir(parents=True)
    # Nine levels below the root: beyond the 8-parent walk cap.
    with pytest.raises(FileNotFoundError):
        resolve_project_root(deep)


def test_get_defaults_on_missing_section_and_key(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    assert gp.get("nonexistent", "key") is None
    assert gp.get("nonexistent", "key", "fallback") == "fallback"
    assert gp.get("project", "no_such_key", "fallback") == "fallback"


def test_delete_and_delete_section(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    gp.delete("display", "width")
    assert "width = 960" not in gp.text
    assert gp.get("display", "width") is None
    gp.delete_section("library")
    lines = gp.text.splitlines()
    assert "[library]" not in lines
    assert "include_dirs = monarch utils" not in lines
    # Unrelated sections survive.
    assert "[project]" in lines
    assert "[display]" in lines


def test_add_dependency_inserts_at_eof_when_project_last(tmp_path):
    path = tmp_path / "project_last.project"
    path.write_text(
        "[display]\n"
        "width = 960\n"
        "\n"
        "[project]\n"
        "title = T\n",
        encoding="utf-8",
    )
    gp = GameProject(path)
    assert gp.add_dependency("https://example.com/a.zip") is True
    lines = gp.text.splitlines()
    assert lines[-1] == "dependencies#0 = https://example.com/a.zip"
    assert gp.get_dependencies() == ["https://example.com/a.zip"]


def test_save_preserves_crlf(tmp_path):
    path = tmp_path / "game.project"
    # write_bytes(): write_text() would translate "\n" to os.linesep,
    # corrupting the CRLF fixture on Windows.
    path.write_bytes(FIXTURE.replace("\n", "\r\n").encode("utf-8"))
    gp = GameProject(path)
    assert gp.get("project", "title") == "My Game"
    gp.set("project", "version", "0.2")
    gp.save()
    # Read bytes: read_text() would normalise CRLF back to LF.
    saved = path.read_bytes().decode("utf-8")
    assert "\r\n" in saved
    assert "\n" not in saved.replace("\r\n", "")
    assert saved.endswith("\r\n")


def test_mutators_reject_newlines_and_bad_sections(tmp_path):
    gp = GameProject(write_fixture(tmp_path))
    with pytest.raises(ValueError, match="single-line strings"):
        gp.set("project", "title", "a\n[project]\ndependencies#0 = https://evil")
    with pytest.raises(ValueError, match="single-line strings"):
        gp.set("bad\nsection", "key", "value")
    with pytest.raises(ValueError, match="must not contain '\\]'"):
        gp.set("sec]ion", "key", "value")
    with pytest.raises(ValueError, match="url must be a single-line string"):
        gp.add_dependency("https://example.com/a\n[project]")
    with pytest.raises(ValueError, match="url must be a single-line string"):
        gp.remove_dependency("https://example.com/a\r")
    # The file is untouched after the rejected calls.
    assert gp.get("project", "title") == "My Game"
    assert gp.get_dependencies() == [URL0, URL1]
