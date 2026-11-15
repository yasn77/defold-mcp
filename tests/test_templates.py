"""Tests for the project scaffolding templates (Task 11, Part B).

Every template renders a valid, Defold-loadable project skeleton: a
``game.project`` whose physics/display defaults match the chosen profile,
a ``main.collection`` that round-trips byte-exactly through the textproto
parser/serializer, a Defold License attribution module, and the ignore
files ``defold_sync_deps`` expects to find.

All tests are hermetic: pure filesystem work, no network and no engine.
"""

import pytest

from pathlib import Path

from defold_mcp.project import GameProject
from defold_mcp.templates import KNOWN_LIBRARY_URLS, render_project
from defold_mcp.textproto import (
    decode_embedded_data,
    parse_textproto,
    serialize_textproto,
)
from defold_mcp.textproto.schemas import ALWAYS_LIST_FIELDS

PROFILES = ("2d_pixel", "2d_hd", "3d_pbr", "minimal")


def _render(tmp_path, profile: str, title: str = "Test Game", **kwargs):
    result = render_project(
        tmp_path / profile, title=title, profile=profile, **kwargs
    )
    return Path(result["root"])


def _as_entries(value):
    """Normalise a repeated field that the decoder may leave as a single dict."""
    if isinstance(value, dict):
        return [value]
    return value


def test_every_profile_renders_core_files(tmp_path):
    for profile in PROFILES:
        root = _render(tmp_path, profile)
        assert (root / "game.project").is_file()
        assert (root / "main" / "main.collection").is_file()
        assert (root / "main" / "main.script").is_file()
        assert (root / "license" / "defold_notice.lua").is_file()
        assert (root / ".gitignore").is_file()
        assert (root / ".defignore").is_file()


def test_game_project_title_and_bootstrap(tmp_path):
    root = _render(tmp_path, "2d_pixel", title="TestQuest")
    project = GameProject.load(root)
    assert project.get("project", "title") == '"TestQuest"'
    assert project.get("bootstrap", "main_collection") == "/main/main.collectionc"
    assert project.get("bootstrap", "render") == "/builtins/render/default.renderc"
    assert (
        project.get("bootstrap", "debug_init_script")
        == "/builtins/scripts/debug_init.scriptc"
    )


@pytest.mark.parametrize("profile", ["2d_pixel", "2d_hd", "minimal"])
def test_2d_profiles_use_2d_physics_defaults(tmp_path, profile):
    root = _render(tmp_path, profile)
    project = GameProject.load(root)
    assert project.get("physics", "scale") == "0.01"
    assert project.get("physics", "gravity_y") == "-1000.0"
    assert project.get("physics", "velocity_threshold") == "100.0"
    assert project.get("physics", "use_fixed_timestep") == "1"
    assert project.get("engine", "fixed_update_frequency") == "60"
    assert project.get("script", "shared_state") == "1"


def test_3d_pbr_profile_uses_3d_physics(tmp_path):
    root = _render(tmp_path, "3d_pbr")
    project = GameProject.load(root)
    assert project.get("physics", "type") == "3D"
    assert project.get("physics", "scale") == "1.0"


@pytest.mark.parametrize("profile", ["2d_pixel", "minimal"])
def test_2d_display_is_960x540(tmp_path, profile):
    root = _render(tmp_path, profile)
    project = GameProject.load(root)
    assert project.get("display", "width") == "960"
    assert project.get("display", "height") == "540"


@pytest.mark.parametrize("profile", ["2d_hd", "3d_pbr"])
def test_hd_display_is_1280x720(tmp_path, profile):
    root = _render(tmp_path, profile)
    project = GameProject.load(root)
    assert project.get("display", "width") == "1280"
    assert project.get("display", "height") == "720"


def test_html5_defaults_present(tmp_path):
    root = _render(tmp_path, "2d_pixel")
    project = GameProject.load(root)
    assert project.get("html5", "scale_mode") == "Stretch"
    assert project.get("html5", "heap_size") == "64"
    assert project.get("html5", "retry_count") == "1000"


def test_main_collection_round_trips_byte_exactly(tmp_path):
    for profile in PROFILES:
        root = _render(tmp_path, profile)
        text = (root / "main" / "main.collection").read_text(encoding="utf-8")
        tree = parse_textproto(text, schema=ALWAYS_LIST_FIELDS)
        assert serialize_textproto(decode_embedded_data(tree)) == text


def test_2d_collection_embeds_sprite_and_script(tmp_path):
    root = _render(tmp_path, "2d_pixel")
    text = (root / "main" / "main.collection").read_text(encoding="utf-8")
    tree = decode_embedded_data(parse_textproto(text, schema=ALWAYS_LIST_FIELDS))
    embedded = {e["id"]: e for e in tree["embedded_instances"]}
    hero = embedded["hero"]["data_ast"]
    sprite = {c["id"]: c for c in _as_entries(hero["embedded_components"])}
    assert sprite["sprite"]["type"] == "sprite"
    assert "idle" in sprite["sprite"]["data"]
    assert "/builtins/materials/sprite.material" in sprite["sprite"]["data"]
    referenced = {c["id"]: c for c in _as_entries(hero["components"])}
    assert referenced["script"]["component"] == "/main/main.script"


def test_3d_collection_embeds_orthographic_camera(tmp_path):
    root = _render(tmp_path, "3d_pbr")
    text = (root / "main" / "main.collection").read_text(encoding="utf-8")
    tree = decode_embedded_data(parse_textproto(text, schema=ALWAYS_LIST_FIELDS))
    embedded = {e["id"]: e for e in tree["embedded_instances"]}
    camera = _as_entries(embedded["camera"]["data_ast"]["embedded_components"])[0]
    assert camera["type"] == "camera"
    assert "orthographic_projection: 0" in camera["data"]


def test_main_script_prints_title_and_notice(tmp_path):
    root = _render(tmp_path, "2d_pixel", title="Pixel Quest")
    script = (root / "main" / "main.script").read_text(encoding="utf-8")
    assert 'go.property("speed", 10)' in script
    assert "Pixel Quest" in script
    assert "license.defold_notice" in script


def test_license_notice_credits_defold(tmp_path):
    root = _render(tmp_path, "minimal")
    notice = (root / "license" / "defold_notice.lua").read_text(encoding="utf-8")
    assert "Defold" in notice
    assert "defold.com/license" in notice
    assert "NOTICE" in notice


def test_monarch_dependency_is_written_as_indexed_key(tmp_path):
    root = _render(tmp_path, "2d_pixel", dependencies=["monarch"])
    project = GameProject.load(root)
    assert project.get("project", "dependencies#0") == KNOWN_LIBRARY_URLS["monarch"]


def test_full_url_dependency_is_kept_verbatim(tmp_path):
    url = "https://example.invalid/archive.zip"
    root = _render(tmp_path, "2d_pixel", dependencies=[url])
    project = GameProject.load(root)
    assert project.get_dependencies() == [url]


def test_unknown_profile_raises(tmp_path):
    with pytest.raises(ValueError):
        render_project(tmp_path / "p", title="Game", profile="bogus")


def test_refuses_non_empty_directory(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        render_project(target, title="Game", profile="minimal")


def test_deftest_runner_is_opt_in(tmp_path):
    plain = _render(tmp_path, "minimal")
    assert not (plain / "test").exists()

    with_deftest = Path(
        render_project(
            tmp_path / "deftest",
            title="Tested",
            profile="minimal",
            with_deftest=True,
        )["root"]
    )
    assert (with_deftest / "test" / "test_runner.script").is_file()
    assert (with_deftest / "test" / "test.collection").is_file()


def test_script_escapes_quotes_in_the_title(tmp_path):
    root = _render(tmp_path, "minimal", title='My "Game"')
    script = (root / "main" / "main.script").read_text(encoding="utf-8")
    assert 'print("My \\"Game\\"")' in script


def test_3d_collection_mounts_the_script_component(tmp_path):
    root = _render(tmp_path, "3d_pbr")
    text = (root / "main" / "main.collection").read_text(encoding="utf-8")
    tree = decode_embedded_data(parse_textproto(text, schema=ALWAYS_LIST_FIELDS))
    embedded = {e["id"]: e for e in tree["embedded_instances"]}
    mounted = {c["id"]: c for c in _as_entries(embedded["camera"]["data_ast"]["components"])}
    assert mounted["script"]["component"] == "/main/main.script"
