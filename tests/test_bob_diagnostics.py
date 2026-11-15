"""Hermetic tests for the bob.jar runner and the headless test runner.

All subprocesses are fake POSIX shell scripts written to ``tmp_path`` —
no real JVM, no real Defold engine, no network.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from defold_mcp import bob_runner, test_runner


# -- helpers -------------------------------------------------------------------


def make_fake_exe(path: Path, script_body: str) -> Path:
    """Write a POSIX shell script as an executable fake binary."""
    path.write_text(f"#!/bin/sh\n{script_body}\n")
    os.chmod(path, 0o755)
    return path


@pytest.fixture
def project(tmp_path) -> Path:
    """A minimal project dir with a ``game.project`` file."""
    p = tmp_path / "project"
    p.mkdir()
    (p / "game.project").write_text("[project]\ntitle = T\n")
    return p


@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Path:
    """Point the cache at an empty temp dir for the duration of a test."""
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("DEFOLD_MCP_CACHE_DIR", str(d))
    return d


def assert_no_process(pattern: str) -> None:
    """Assert no process whose command line matches ``pattern`` lingers."""
    result = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    )
    assert result.returncode != 0, f"lingering process matching {pattern!r}: {result.stdout!r}"


# -- parse_bob_diagnostics ------------------------------------------------------


def test_parse_bob_diagnostics_errors_and_warnings():
    output = (
        "Building project...\n"
        "ERROR /main/player.script:12: '=' expected near 'x'\n"
        "WARNING /main/main.collection: unable to parse\n"
        "WARNING: A restricted method in java.lang.System has been called\n"
        "WARNING: Consider enabling the ability to suppress warnings\n"
        "WARNING: A java.lang.instrument agent has been loaded dynamically\n"
        "BUILD OK\n"
    )
    diagnostics = bob_runner.parse_bob_diagnostics(output)
    assert diagnostics == [
        {
            "severity": "error",
            "resource": "/main/player.script",
            "line": 12,
            "message": "'=' expected near 'x'",
        },
        {
            "severity": "warning",
            "resource": "/main/main.collection",
            "line": None,
            "message": "unable to parse",
        },
    ]


def test_parse_diagnostics_colon_prefix_tolerated():
    diagnostics = bob_runner.parse_bob_diagnostics("ERROR: /x/a.script:3: bad\n")
    assert diagnostics == [
        {
            "severity": "error",
            "resource": "/x/a.script",
            "line": 3,
            "message": "bad",
        }
    ]


# -- summarize_build_report -----------------------------------------------------


def test_summarize_build_report_top10_and_total(tmp_path):
    entries = []
    for i in range(1, 13):
        # Mix both key shapes: odd entries use "resource", even use "path".
        key = "resource" if i % 2 else "path"
        entries.append({key: f"/res/{i:02d}.png", "size": i * 100})
    report = json.dumps({"resources": entries})

    summary = bob_runner.summarize_build_report(report)
    assert summary["total_size"] == sum(i * 100 for i in range(1, 13))
    assert len(summary["resources"]) == 10
    sizes = [r["size"] for r in summary["resources"]]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes == [i * 100 for i in range(12, 2, -1)]
    assert summary["resources"][0]["path"] == "/res/12.png"

    # A Path to the report file is accepted too.
    report_file = tmp_path / "report.json"
    report_file.write_text(report)
    assert bob_runner.summarize_build_report(report_file) == summary


def test_summarize_build_report_rejects_shapeless():
    with pytest.raises(ValueError, match="resources"):
        bob_runner.summarize_build_report("{}")


# -- run_bob --------------------------------------------------------------------


def test_run_bob_command_construction(project, cache_dir, tmp_path):
    args_file = tmp_path / "bob_args.txt"
    cwd_file = tmp_path / "bob_cwd.txt"
    java = make_fake_exe(
        tmp_path / "java",
        f"pwd > {cwd_file}\n"
        f'for a in "$@"; do printf "%s\\n" "$a"; done > {args_file}',
    )
    bob_jar = tmp_path / "bob.jar"
    bob_jar.write_bytes(b"PK-fake")
    report_path = project / "report.json"
    report_path.write_text(
        json.dumps({"resources": [{"resource": "/a.png", "size": 7}]})
    )

    result = bob_runner.run_bob(
        project,
        ["build"],
        java=str(java),
        bob_jar=str(bob_jar),
        build_report_path=str(report_path),
    )

    args = args_file.read_text().splitlines()
    assert args[0] == "-jar"
    assert args[1] == str(bob_jar)
    assert args[2] == "build"
    i = args.index("--resource-cache-local")
    assert args[i + 1] == str(cache_dir / "resource-cache")
    i = args.index("--build-report-json")
    assert args[i + 1] == str(report_path)
    assert cwd_file.read_text().strip() == str(project)

    assert result.exit_code == 0
    assert result.ok is True
    assert result.diagnostics == []
    assert result.build_report == {"total_size": 7, "resources": [{"path": "/a.png", "size": 7}]}
    assert (project / "game.project").read_text() == "[project]\ntitle = T\n"


def test_run_bob_diagnostics_parsed(project, cache_dir, tmp_path):
    java = make_fake_exe(
        tmp_path / "java",
        "echo \"ERROR /main/player.script:12: '=' expected near 'x'\"\nexit 1",
    )
    bob_jar = tmp_path / "bob.jar"
    bob_jar.write_bytes(b"PK-fake")

    result = bob_runner.run_bob(
        project, ["build"], java=str(java), bob_jar=str(bob_jar)
    )

    assert result.exit_code == 1
    assert result.ok is False
    assert result.diagnostics == [
        {
            "severity": "error",
            "resource": "/main/player.script",
            "line": 12,
            "message": "'=' expected near 'x'",
        }
    ]


def test_run_bob_java_missing_clear_error(project, cache_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(bob_runner.toolchain, "java_executable", lambda: None)
    bob_jar = tmp_path / "bob.jar"
    bob_jar.write_bytes(b"PK-fake")

    with pytest.raises(RuntimeError, match="defold_setup_toolchain\\(install_jdk25=True\\)"):
        bob_runner.run_bob(project, ["build"], java=None, bob_jar=str(bob_jar))


def test_run_bob_timeout_kills_and_raises(project, cache_dir, tmp_path):
    java = make_fake_exe(tmp_path / "java", "echo starting\nexec sleep 300")
    bob_jar = tmp_path / "bob.jar"
    bob_jar.write_bytes(b"PK-fake")

    start = time.monotonic()
    with pytest.raises(bob_runner.BobTimeoutError) as excinfo:
        bob_runner.run_bob(
            project, ["build"], java=str(java), bob_jar=str(bob_jar), timeout_s=1
        )
    elapsed = time.monotonic() - start

    assert elapsed < 5
    assert "starting" in excinfo.value.partial_output
    assert_no_process("sleep 300")


def test_run_bob_missing_report_file_yields_warning(project, cache_dir, tmp_path):
    java = make_fake_exe(tmp_path / "java", "echo done\nexit 0")
    bob_jar = tmp_path / "bob.jar"
    bob_jar.write_bytes(b"PK-fake")
    report_path = project / "no-such-report.json"

    result = bob_runner.run_bob(
        project,
        ["build"],
        java=str(java),
        bob_jar=str(bob_jar),
        build_report_path=str(report_path),
    )

    assert result.exit_code == 0
    assert result.build_report is None
    assert any(
        d["severity"] == "warning" and "build report" in d["message"]
        for d in result.diagnostics
    )


def test_run_bob_warnings_keep_ok(project, cache_dir, tmp_path):
    java = make_fake_exe(
        tmp_path / "java", 'echo "WARNING /main/main.collection: unable to parse"\nexit 0'
    )
    bob_jar = tmp_path / "bob.jar"
    bob_jar.write_bytes(b"PK-fake")
    result = bob_runner.run_bob(project, ["build"], java=str(java), bob_jar=str(bob_jar))
    assert result.ok is True
    assert result.diagnostics[0]["severity"] == "warning"


def test_summarize_build_report_type_and_shape_errors():
    with pytest.raises(TypeError, match="str, bytes, or Path"):
        bob_runner.summarize_build_report(1234)
    with pytest.raises(ValueError, match="resources"):
        bob_runner.summarize_build_report("[1, 2, 3]")


# -- parse_test_stream ------------------------------------------------------------


def test_parse_test_stream_json_events():
    output = (
        'TEST {"run": "8f13", "event": "test_start", "name": "math.add"}\n'
        'TEST {"run": "8f13", "event": "test_end", "name": "math.add", "status": "pass"}\n'
        'TEST {"run": "8f13", "event": "test_start", "name": "math.sub"}\n'
        'TEST {"run": "8f13", "event": "test_end", "name": "math.sub", "status": "pass"}\n'
        'TEST {"run": "8f13", "event": "test_start", "name": "math.mul"}\n'
        'TEST {"run": "8f13", "event": "test_end", "name": "math.mul", "status": "pass"}\n'
        'TEST {"run": "8f13", "event": "test_start", "name": "math.div"}\n'
        'TEST {"run": "8f13", "event": "test_end", "name": "math.div", "status": "pass"}\n'
        'TEST {"run": "8f13", "event": "suite_end", "status": "pass", "passed": 4, "failed": 0}\n'
    )
    summary = test_runner.parse_test_stream(output)
    assert summary["run"] == "8f13"
    assert summary["passed"] == 4
    assert summary["failed"] == 0
    assert summary["status"] == "pass"
    assert summary["rejected_events"] == 0
    assert [t["name"] for t in summary["tests"]] == [
        "math.add",
        "math.sub",
        "math.mul",
        "math.div",
    ]
    assert all(t["status"] == "pass" for t in summary["tests"])
    assert summary["stdout_tail"].rstrip().endswith('failed": 0}')


def test_parse_test_stream_rejects_foreign_run_ids_and_deftest_fallback():
    output = (
        'TEST {"run": "8f13", "event": "suite_end", "status": "pass", "passed": 4, "failed": 0}\n'
        'TEST {"run": "9999", "event": "suite_end", "status": "fail", "passed": 0, "failed": 9}\n'
    )
    summary = test_runner.parse_test_stream(output, expected_run="8f13")
    assert summary["run"] == "8f13"
    assert summary["passed"] == 4
    assert summary["failed"] == 0
    assert summary["rejected_events"] == 1

    # DefTest text fallback: no TEST events at all.
    fallback = test_runner.parse_test_stream(
        "Running tests...\nSuccess: 4 passed, 0 failed\n"
    )
    assert fallback["run"] is None
    assert fallback["passed"] == 4
    assert fallback["failed"] == 0
    assert fallback["status"] == "pass"
    assert fallback["tests"] == []


def test_parse_test_stream_rejects_runless_events_after_run_fixed():
    output = (
        'TEST {"event": "test_end", "name": "a", "status": "pass"}\n'
        'TEST {"run": "r1", "event": "test_end", "name": "b", "status": "pass"}\n'
        'TEST {"event": "test_end", "name": "c", "status": "fail"}\n'
        'TEST {"run": "r1", "event": "suite_end", "status": "pass", "passed": 1, "failed": 0}\n'
    )
    summary = test_runner.parse_test_stream(output)
    assert summary["run"] == "r1"
    assert summary["rejected_events"] == 1
    assert [t["name"] for t in summary["tests"]] == ["a", "b"]
    assert summary["passed"] == 1
    assert summary["failed"] == 0


def test_compiled_collection_path_forms():
    assert test_runner._compiled_collection_path("/t/a.collection") == "/t/a.collectionc"
    assert test_runner._compiled_collection_path("/t/a.collectionc") == "/t/a.collectionc"
    assert test_runner._compiled_collection_path("/t/a") == "/t/a"


def test_parse_test_stream_derives_totals_without_suite_end():
    output = (
        'TEST {"run": "r1", "event": "test_end", "name": "a", "status": "pass"}\n'
        'TEST {"run": "r1", "event": "test_end", "name": "b", "status": "fail"}\n'
    )
    summary = test_runner.parse_test_stream(output)
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["status"] == "fail"


def test_parse_test_stream_stdout_tail_truncated_to_2000():
    summary = test_runner.parse_test_stream("x" * 5000)
    assert len(summary["stdout_tail"]) == 2000


# -- run_headless_tests -----------------------------------------------------------


def test_run_headless_tests_command_and_immutability(project, cache_dir, tmp_path):
    args_file = tmp_path / "engine_args.txt"
    engine = make_fake_exe(
        tmp_path / "dmengine_headless",
        f'for a in "$@"; do printf "%s\\n" "$a"; done > {args_file}\n'
        'echo \'TEST {"run": "abcd", "event": "suite_end", "status": "pass", "passed": 4, "failed": 0}\'',
    )
    before = (project / "game.project").read_bytes()

    summary = test_runner.run_headless_tests(
        project,
        "/test/math_test.collection",
        engine=str(engine),
    )

    args = args_file.read_text().splitlines()
    assert args[0] == "--config=bootstrap.main_collection=/test/math_test.collectionc"
    assert args[-1] == str(project)
    assert (project / "game.project").read_bytes() == before

    assert summary["ok"] is True
    assert summary["exit_code"] == 0
    assert summary["passed"] == 4
    assert summary["failed"] == 0
    assert summary["status"] == "pass"
    assert summary["run"] == "abcd"


def test_run_headless_tests_bool_config_overrides_lowercased(project, cache_dir, tmp_path):
    args_file = tmp_path / "engine_args.txt"
    engine = make_fake_exe(
        tmp_path / "dmengine_headless",
        f'for a in "$@"; do printf "%s\\n" "$a"; done > {args_file}',
    )
    test_runner.run_headless_tests(
        project,
        "/test/t.collection",
        engine=str(engine),
        config_overrides={
            "graphics.vsync": True,
            "audio.enabled": False,
            "graphics.log_level": "debug",
        },
    )
    args = args_file.read_text().splitlines()
    assert "--config=graphics.vsync=true" in args
    assert "--config=audio.enabled=false" in args
    assert "--config=graphics.log_level=debug" in args


def test_run_headless_tests_nonzero_exit_not_ok(project, cache_dir, tmp_path):
    engine = make_fake_exe(
        tmp_path / "dmengine_headless",
        'echo \'TEST {"run": "abcd", "event": "suite_end", "status": "pass", "passed": 1, "failed": 0}\'\n'
        "exit 1",
    )
    summary = test_runner.run_headless_tests(
        project, "/t/a.collection", engine=str(engine)
    )
    assert summary["ok"] is False
    assert summary["exit_code"] == 1


def test_run_headless_tests_timeout_kills(project, cache_dir, tmp_path):
    engine = make_fake_exe(
        tmp_path / "dmengine_headless", "echo starting\nexec sleep 300"
    )

    start = time.monotonic()
    with pytest.raises(test_runner.TestTimeoutError) as excinfo:
        test_runner.run_headless_tests(
            project,
            "/test/math_test.collection",
            engine=str(engine),
            timeout_s=1,
        )
    elapsed = time.monotonic() - start

    assert elapsed < 5
    assert "starting" in excinfo.value.partial_output
    assert_no_process("sleep 300")
