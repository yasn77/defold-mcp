"""Hermetic tests for the toolchain resolver and the ``.deps/`` synchroniser.

All HTTP goes through ``httpx.MockTransport``; all archives are built in
memory. No test touches the real network or the host Java installation.
"""

import io
import json
import os
import re
import stat
import tarfile
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from defold_mcp import config, deps, docs_index, toolchain
from defold_mcp.project import GameProject

STABLE_SHA1 = "ee283d4dad9fa451dd6399d83602e1dc25133681"
STABLE_INFO = {"version": "1.14.1", "sha1": STABLE_SHA1}


# -- helpers -----------------------------------------------------------------


def make_zip(entries: dict[str, bytes | None]) -> bytes:
    """Build an in-memory zip; a ``None`` value writes a directory entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            if data is None:
                zf.writestr(name if name.endswith("/") else name + "/", b"")
            else:
                zf.writestr(name, data)
    return buf.getvalue()


def make_jdk_tar() -> bytes:
    """Build an in-memory Adoptium-style tar.gz with a single top dir.

    ``bin/java`` carries mode 0o755, as in the real Adoptium archive: the
    extractor must preserve it or the JDK is unusable.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in {
            "jdk-25.0.1+9/bin/java": b"#!/bin/sh\necho fake\n",
            "jdk-25.0.1+9/release": b"JAVA_VERSION='25'\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith("/bin/java") else 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_jdk_zip(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory Adoptium-style zip (entry names verbatim)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def fake_java(directory: Path, version_line: str) -> Path:
    """Write a fake ``bin/java`` shell script under ``directory``."""
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    java = bin_dir / "java"
    java.write_text(f"#!/bin/sh\necho '{version_line}' >&2\n")
    java.chmod(0o755)
    return java


@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Path:
    """Point the cache at an empty temp dir for the duration of a test."""
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("DEFOLD_MCP_CACHE_DIR", str(d))
    return d


@pytest.fixture
def mock_transport():
    """Return a factory: URL->Response map to (client, transport with .calls)."""

    def _make(responses: dict[str, httpx.Response]):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            if url in responses:
                return responses[url]
            return httpx.Response(404, json={"error": "not found"})

        transport = httpx.MockTransport(handler)
        transport.calls = calls
        client = httpx.Client(transport=transport)
        return client, transport

    return _make


# -- resolve_version ----------------------------------------------------------


def test_resolve_version_stable_parses_and_caches(cache_dir, mock_transport):
    url = "https://d.defold.com/stable/info.json"
    client, transport = mock_transport({url: httpx.Response(200, json=STABLE_INFO)})

    result = toolchain.resolve_version("stable", client=client)
    assert result == STABLE_INFO
    assert transport.calls == [url]

    # Within the TTL: served from cache, no re-request.
    again = toolchain.resolve_version("stable", client=client)
    assert again == STABLE_INFO
    assert len(transport.calls) == 1

    # force=True bypasses the cache.
    forced = toolchain.resolve_version("stable", client=client, force=True)
    assert forced == STABLE_INFO
    assert len(transport.calls) == 2

    # TTL expiry: age the cached entry past one hour.
    cache_file = cache_dir / "versions.json"
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["stable"]["fetched_at"] = time.time() - 3700
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    expired = toolchain.resolve_version("stable", client=client)
    assert expired == STABLE_INFO
    assert len(transport.calls) == 3


def test_resolve_version_pinned_spec(cache_dir, mock_transport):
    url = "https://d.defold.com/1.14.1/info.json"

    # 404 with no matching channel falls back to version-keyed mode with
    # sha1 None; the degraded result is never cached. The channel probes
    # are attempted first (see the matching-channel test below).
    client, transport = mock_transport({url: httpx.Response(404)})
    result = toolchain.resolve_version("1.14.1", client=client)
    assert result == {"version": "1.14.1", "sha1": None}
    assert transport.calls[0] == url
    assert not (cache_dir / "versions.json").exists()

    # A 200 resolves the sha1 (fresh client).
    client2, transport2 = mock_transport(
        {url: httpx.Response(200, json=STABLE_INFO)}
    )
    result2 = toolchain.resolve_version("1.14.1", client=client2)
    assert result2 == STABLE_INFO
    assert transport2.calls == [url]


def test_resolve_version_pinned_spec_adopts_the_matching_channel(
    cache_dir, mock_transport
):
    # d.defold.com serves CHANNELS, not pinned dotted versions: the pinned
    # URL 404s even for a released version. The matching channel's sha1 is
    # adopted so engines (which have no version-keyed GitHub fallback)
    # resolve through the /archive/<sha1>/ URLs.
    pinned_url = "https://d.defold.com/1.14.1/info.json"
    client, transport = mock_transport(
        {
            pinned_url: httpx.Response(404),
            "https://d.defold.com/stable/info.json": httpx.Response(
                200, json=STABLE_INFO
            ),
        }
    )

    result = toolchain.resolve_version("1.14.1", client=client)
    assert result == STABLE_INFO
    assert transport.calls[0] == pinned_url

    # The adopted resolution is cached under the pinned spec: no more
    # requests on the next call.
    calls = len(transport.calls)
    assert toolchain.resolve_version("1.14.1", client=client) == STABLE_INFO
    assert len(transport.calls) == calls


def test_resolve_version_pinned_spec_ignores_a_mismatched_channel(
    cache_dir, mock_transport
):
    # A channel carrying a DIFFERENT version must not satisfy the pin.
    pinned_url = "https://d.defold.com/1.14.1/info.json"
    client, _ = mock_transport(
        {
            pinned_url: httpx.Response(404),
            "https://d.defold.com/stable/info.json": httpx.Response(
                200, json={"version": "1.15.0", "sha1": "other"}
            ),
        }
    )

    assert toolchain.resolve_version("1.14.1", client=client) == {
        "version": "1.14.1",
        "sha1": None,
    }


def test_resolve_version_pinned_spec_prefers_stable_over_beta(
    cache_dir, mock_transport
):
    # A version can be live on more than one channel with DIFFERENT sha1s
    # (1.14.1 was reported by both ``beta`` and ``stable``). A pinned
    # version means the released build, so ``stable`` is probed first and
    # its sha1 wins; ``beta`` is never consulted.
    pinned_url = "https://d.defold.com/1.14.1/info.json"
    beta_url = "https://d.defold.com/beta/info.json"
    stable_url = "https://d.defold.com/stable/info.json"
    client, transport = mock_transport(
        {
            pinned_url: httpx.Response(404),
            beta_url: httpx.Response(
                200, json={"version": "1.14.1", "sha1": "beta-sha1"}
            ),
            stable_url: httpx.Response(200, json=STABLE_INFO),
        }
    )

    result = toolchain.resolve_version("1.14.1", client=client)

    assert result == STABLE_INFO
    assert result["sha1"] == STABLE_SHA1
    assert transport.calls == [pinned_url, stable_url]


def test_resolve_version_channel_non200_raises(cache_dir, mock_transport):
    # The 404 fallback is reserved for pinned specs: a channel that d.defold.com
    # does not serve must raise, and the failure must never be cached.
    url = "https://d.defold.com/stable/info.json"
    client, _ = mock_transport({url: httpx.Response(404)})

    with pytest.raises(ValueError, match="stable"):
        toolchain.resolve_version("stable", client=client)
    assert not (cache_dir / "versions.json").exists()


def test_resolve_version_pinned_server_error_raises(cache_dir, mock_transport):
    # Only a 404 falls back for a pinned spec; other errors raise.
    url = "https://d.defold.com/1.14.1/info.json"
    client, _ = mock_transport({url: httpx.Response(500)})

    with pytest.raises(ValueError, match="1.14.1"):
        toolchain.resolve_version("1.14.1", client=client)
    assert not (cache_dir / "versions.json").exists()


def test_resolve_version_missing_version_key_raises(cache_dir, mock_transport):
    # A 200 whose info.json lacks the "version" key raises, naming the URL.
    url = "https://d.defold.com/stable/info.json"
    client, _ = mock_transport({url: httpx.Response(200, json={"sha1": "abc"})})

    with pytest.raises(ValueError, match="info.json"):
        toolchain.resolve_version("stable", client=client)
    assert not (cache_dir / "versions.json").exists()


# -- check_java ----------------------------------------------------------------


def test_check_java_rejects_21(cache_dir, monkeypatch):
    jdk = cache_dir / "fake-jdk"
    fake_java(jdk, 'openjdk version "21.0.12.1"')
    monkeypatch.setenv("JAVA_HOME", str(jdk))

    result = toolchain.check_java()
    assert result["ok"] is False
    assert result["major"] == 21
    assert result["source"] == "JAVA_HOME"
    assert "defold_setup_toolchain(install_jdk25=True)" in result["diagnostic"]
    assert "/bob" in result["diagnostic"]


def test_check_java_accepts_25(cache_dir, monkeypatch):
    jdk = cache_dir / "fake-jdk"
    fake_java(jdk, 'openjdk version "25"')
    monkeypatch.setenv("JAVA_HOME", str(jdk))

    result = toolchain.check_java()
    assert result["ok"] is True
    assert result["major"] == 25
    assert result["diagnostic"] is None


def test_check_java_prefers_portable(cache_dir, monkeypatch):
    # The portable JDK is probed before JAVA_HOME, aligning check_java with
    # java_executable: a stale JAVA_HOME (Java 21) must not shadow a
    # qualifying portable JDK 25.
    jdk21 = cache_dir / "jdk-21"
    fake_java(jdk21, 'openjdk version "21.0.12.1"')
    monkeypatch.setenv("JAVA_HOME", str(jdk21))
    fake_java(cache_dir / "jdk-25", 'openjdk version "25"')
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)

    result = toolchain.check_java()
    assert result["ok"] is True
    assert result["major"] == 25
    assert result["source"] == "portable"
    assert result["diagnostic"] is None


def test_check_java_broken_portable_falls_back_to_java_home(
    cache_dir, monkeypatch
):
    # A broken portable JDK (bin/java present but non-functional) must not
    # shadow a qualifying JAVA_HOME Java: check_java probes every existing
    # candidate and returns the first one that qualifies, agreeing with
    # java_executable.
    fake_java(cache_dir / "jdk-25", "")  # prints nothing parseable
    jdk_home = cache_dir / "jdk-home"
    fake_java(jdk_home, 'openjdk version "25"')
    monkeypatch.setenv("JAVA_HOME", str(jdk_home))
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)

    result = toolchain.check_java()
    assert result["ok"] is True
    assert result["major"] == 25
    assert result["source"] == "JAVA_HOME"
    assert result["diagnostic"] is None


def test_check_java_missing(cache_dir, monkeypatch):
    monkeypatch.setenv("JAVA_HOME", "")
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)

    result = toolchain.check_java()
    assert result["ok"] is False
    assert result["major"] is None
    assert result["java_path"] is None
    assert result["source"] is None


# -- download_artifact ----------------------------------------------------------


def test_download_artifact_magic_check(tmp_path, mock_transport):
    url = "https://example.com/bob.jar"
    client, _ = mock_transport({url: httpx.Response(200, content=b"PK\x03\x04rest")})
    dest = tmp_path / "out" / "bob.jar"

    result = toolchain.download_artifact(url, dest, client=client, expected_magic=b"PK")
    assert result == dest
    assert dest.read_bytes() == b"PK\x03\x04rest"
    # Atomic: no .tmp left behind on success.
    assert not (tmp_path / "out" / "bob.jar.tmp").exists()

    bad_url = "https://example.com/bad.jar"
    client2, _ = mock_transport({bad_url: httpx.Response(200, content=b"MZ\x90\x00")})
    with pytest.raises(ValueError, match="PK"):
        toolchain.download_artifact(
            bad_url, tmp_path / "bad.jar", client=client2, expected_magic=b"PK"
        )
    assert not (tmp_path / "bad.jar").exists()


def test_download_artifact_reuses_a_cached_artifact(tmp_path, mock_transport):
    # Re-resolving the toolchain happens on every compile/test/bundle call;
    # an already-downloaded artifact must not be re-fetched.
    url = "https://example.com/bob.jar"
    client, transport = mock_transport(
        {url: httpx.Response(200, content=b"PK\x03\x04bob")}
    )
    dest = tmp_path / "out" / "bob.jar"

    toolchain.download_artifact(url, dest, client=client, expected_magic=b"PK")
    assert len(transport.calls) == 1

    toolchain.download_artifact(url, dest, client=client, expected_magic=b"PK")
    assert len(transport.calls) == 1, "the cached artifact was re-downloaded"
    assert dest.read_bytes() == b"PK\x03\x04bob"


def test_download_artifact_redownloads_a_corrupt_cache_entry(
    tmp_path, mock_transport
):
    url = "https://example.com/bob.jar"
    client, transport = mock_transport(
        {url: httpx.Response(200, content=b"PK\x03\x04bob")}
    )
    dest = tmp_path / "bob.jar"
    dest.write_bytes(b"MZnot-a-zip")

    toolchain.download_artifact(url, dest, client=client, expected_magic=b"PK")
    assert len(transport.calls) == 1
    assert dest.read_bytes() == b"PK\x03\x04bob"


def test_default_http_clients_follow_redirects(monkeypatch):
    # Every Defold distribution host redirects (d.defold.com/archive 301,
    # GitHub release assets 302, the Adoptium API 307); a bare client would
    # raise on the redirect response instead of fetching the artifact.
    seen: list[dict] = []

    class _NoopResponse:
        content = b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self) -> None:
            pass

        def iter_bytes(self):
            return iter(())

    class _NoopClient:
        def close(self) -> None:
            pass

        def get(self, url):  # noqa: ANN001, ANN201 - stub
            return _NoopResponse()

        def stream(self, method, url):  # noqa: ANN001, ANN201 - stub
            return _NoopResponse()

    def _factory(**kwargs):
        seen.append(kwargs)
        return _NoopClient()

    monkeypatch.setattr(httpx, "Client", _factory)

    toolchain._client_or_default(None)
    docs_index.DocsIndex._get_bytes("https://example.com/x", client=None)
    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs["follow_redirects"] is True
        assert isinstance(kwargs["timeout"], httpx.Timeout)


# -- extract_zip -----------------------------------------------------------------


def test_extract_zip_slip_rejected(tmp_path):
    zip_bytes = make_zip({"../../evil.txt": b"evil", "/etc/evil": b"evil2"})
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="evil"):
        deps.extract_zip(zip_bytes, dest)

    # Destination stays clean: no files written, no escape outside.
    if dest.exists():
        assert list(dest.rglob("*")) == []
    assert not (tmp_path / "evil.txt").exists()


def test_extract_zip_absolute_entry_rejected(tmp_path):
    # A zip whose only entry is an absolute path is rejected wholesale.
    zip_bytes = make_zip({"/etc/evil": b"evil"})
    dest = tmp_path / "dest"

    with pytest.raises(ValueError, match="/etc/evil"):
        deps.extract_zip(zip_bytes, dest)

    if dest.exists():
        assert list(dest.rglob("*")) == []
    assert not Path("/etc/evil").exists()


def test_dependency_name_rejects_dot_segments():
    # "." and ".." segments are unusable directory names: fall back to the
    # short sha1 hash of the URL.
    for url in ("https://example.com/..", "https://example.com/."):
        name = deps._dependency_name(url)
        assert name not in (".", "..", "")
        assert re.fullmatch(r"[0-9a-f]{12}", name)
    # A normal segment is still used verbatim.
    assert deps._dependency_name("https://example.com/monarch.zip") == "monarch"
    assert deps._dependency_name("https://example.com/monarch") == "monarch"


# -- sync_project_deps -------------------------------------------------------------


def test_sync_project_deps_end_to_end(tmp_path, mock_transport):
    root = tmp_path / "project"
    root.mkdir()
    (root / "game.project").write_text("[project]\n", encoding="utf-8")
    gp = GameProject(root / "game.project")
    gp.set("project", "dependencies#0", "https://example.com/monarch.zip")
    gp.save()
    (root / ".gitignore").write_text("builtins\n*.tmp\n", encoding="utf-8")

    dep_zip = make_zip(
        {
            "monarch/monarch.lua": b"return {}\n",
            "monarch/sub/": None,
            "monarch/sub/deep.txt": b"deep\n",
            "monarch/game.project": b"[library]\ninclude_dirs = monarch utils\n",
        }
    )
    builtins_zip = make_zip(
        {
            "builtins/manifests/android/android.manifest": b"<manifest/>\n",
            "builtins/materials/sprite.material": b"material\n",
        }
    )
    responses = {
        "https://d.defold.com/stable/info.json": httpx.Response(
            200, json=STABLE_INFO
        ),
        "https://example.com/monarch.zip": httpx.Response(200, content=dep_zip),
        f"https://d.defold.com/archive/{STABLE_SHA1}/engine/share/builtins.zip": httpx.Response(
            200, content=builtins_zip
        ),
    }
    client, _ = mock_transport(responses)

    result = deps.sync_project_deps(root, client=client)

    assert result["dependencies"] == ["monarch"]
    assert result["builtins"] is True
    assert result["ignore_fixed"] is True

    # Dependency unpacked under .deps/monarch/.
    assert (root / ".deps" / "monarch" / "monarch.lua").read_bytes() == b"return {}\n"
    assert (root / ".deps" / "monarch" / "sub" / "deep.txt").exists()

    # Builtins unpacked under .deps/builtins/.
    assert (root / ".deps" / "builtins" / "materials" / "sprite.material").exists()
    assert (
        root / ".deps" / "builtins" / "manifests" / "android" / "android.manifest"
    ).exists()

    # The library's own game.project is ignored: it stays inside the
    # dependency and the user's file is untouched.
    user_project = (root / "game.project").read_text(encoding="utf-8")
    assert "dependencies#0" in user_project
    assert "include_dirs" not in user_project
    library_project = (root / ".deps" / "monarch" / "game.project").read_text(
        encoding="utf-8"
    )
    assert "include_dirs = monarch utils" in library_project

    # .defignore created with /.deps.
    defignore = (root / ".defignore").read_text(encoding="utf-8")
    assert "/.deps" in defignore.splitlines()

    # .gitignore: bare "builtins" rewritten to "/builtins", /.deps appended,
    # other lines untouched.
    gitignore_lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/builtins" in gitignore_lines
    assert "builtins" not in gitignore_lines
    assert "*.tmp" in gitignore_lines
    assert "/.deps" in gitignore_lines


def test_sync_deps_empty_project(tmp_path, mock_transport):
    root = tmp_path / "project"
    root.mkdir()
    (root / "game.project").write_text("[project]\n", encoding="utf-8")
    gp = GameProject(root / "game.project")
    gp.set("project", "name", "empty-game")
    gp.save()

    builtins_zip = make_zip({"manifests/android/android.manifest": b"<manifest/>\n"})
    responses = {
        "https://d.defold.com/stable/info.json": httpx.Response(
            200, json=STABLE_INFO
        ),
        f"https://d.defold.com/archive/{STABLE_SHA1}/engine/share/builtins.zip": httpx.Response(
            200, content=builtins_zip
        ),
    }
    client, _ = mock_transport(responses)

    result = deps.sync_project_deps(root, client=client)

    assert result["dependencies"] == []
    assert result["builtins"] is True
    assert result["ignore_fixed"] is True
    assert (root / ".deps" / "builtins" / "manifests" / "android" / "android.manifest").exists()
    assert "/.deps" in (root / ".defignore").read_text(encoding="utf-8").splitlines()


# -- install_jdk25 -----------------------------------------------------------------


def test_install_jdk25_idempotent_and_mocked(cache_dir, mock_transport):
    url = toolchain.adoptium_jdk_url()
    client, transport = mock_transport({url: httpx.Response(200, content=make_jdk_tar())})

    path = toolchain.install_jdk25(client=client)
    assert path == cache_dir / "jdk-25"
    assert (path / "bin" / "java").exists()
    assert (path / "release").read_text(encoding="utf-8") == "JAVA_VERSION='25'\n"
    assert transport.calls == [url]

    # Idempotent: second call returns immediately without downloading.
    path2 = toolchain.install_jdk25(client=client)
    assert path2 == path
    assert transport.calls == [url]


def test_extract_jdk_tar_preserves_the_executable_bit(tmp_path):
    # Extraction writes file contents only; without the archive's mode bits
    # ``bin/java`` lands 0o644 and cannot be run.
    if os.name != "posix":
        pytest.skip("POSIX permission bits only")
    archive = tmp_path / "jdk.tar.gz"
    archive.write_bytes(make_jdk_tar())
    target = tmp_path / "jdk-25"

    toolchain._extract_jdk_archive(archive, target)

    java = target / "bin" / "java"
    assert java.is_file()
    assert os.access(java, os.X_OK), "bin/java lost its executable bit"
    assert stat.S_IMODE(java.stat().st_mode) == 0o755
    # A non-executable member keeps its own (non-executable) bits.
    assert stat.S_IMODE((target / "release").stat().st_mode) == 0o644


def test_install_jdk25_repairs_a_non_executable_jdk(cache_dir, mock_transport):
    # A cache left by the older extraction bug has a 0o644 ``bin/java``;
    # install_jdk25 must not treat that as a usable JDK.
    if os.name != "posix":
        pytest.skip("POSIX permission bits only")
    broken = cache_dir / "jdk-25" / "bin"
    broken.mkdir(parents=True)
    java = broken / "java"
    java.write_text("#!/bin/sh\necho fake\n")
    java.chmod(0o644)

    url = toolchain.adoptium_jdk_url()
    client, transport = mock_transport({url: httpx.Response(200, content=make_jdk_tar())})

    path = toolchain.install_jdk25(client=client)

    assert path == cache_dir / "jdk-25"
    assert transport.calls == [url], "the broken JDK was not re-installed"
    assert os.access(path / "bin" / "java", os.X_OK)


def _make_jdk_tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_jdk25_tar_path_escape_rejected(cache_dir, mock_transport):
    # A ``..`` member is rejected before anything is written.
    archive = _make_jdk_tar(
        {
            "jdk-25.0.1+9/bin/java": b"#!/bin/sh\necho fake\n",
            "../evil.txt": b"evil\n",
        }
    )
    url = toolchain.adoptium_jdk_url()
    client, _ = mock_transport({url: httpx.Response(200, content=archive)})

    with pytest.raises(ValueError, match="evil"):
        toolchain.install_jdk25(client=client)

    assert not (cache_dir / "evil.txt").exists()
    assert not (cache_dir / "jdk-25" / "bin" / "java").exists()


def test_install_jdk25_zip_path_escape_rejected(cache_dir, tmp_path):
    # The zip branch (Windows archives) rejects unsafe entries before
    # anything is written.
    archive = tmp_path / "jdk-25-download.zip"
    archive.write_bytes(
        make_jdk_zip(
            {
                "jdk-25.0.1+9/bin/java.exe": b"MZ\x90",
                "../../evil.txt": b"evil\n",
            }
        )
    )

    with pytest.raises(ValueError, match="evil"):
        toolchain._extract_jdk_archive(archive, cache_dir / "jdk-25")

    # ``../../evil.txt`` would land two levels above the target.
    assert not (tmp_path / "evil.txt").exists()
    assert not (cache_dir / "evil.txt").exists()
    assert not (cache_dir / "jdk-25" / "bin" / "java.exe").exists()


def test_install_jdk25_zip_backslash_traversal_rejected(cache_dir, tmp_path):
    # A backslash in a member name is a path separator on Windows and can
    # smuggle ".." traversal past the forward-slash-only check.
    archive = tmp_path / "jdk-25-download.zip"
    archive.write_bytes(
        make_jdk_zip(
            {
                "jdk-25.0.1+9/bin/java.exe": b"MZ\x90",
                "jdk-25.0.1+9/x\\..\\..\\evil.txt": b"evil\n",
            }
        )
    )

    with pytest.raises(ValueError, match="evil"):
        toolchain._extract_jdk_archive(archive, cache_dir / "jdk-25")

    # Nothing written outside the target.
    assert not (tmp_path / "evil.txt").exists()
    assert not (cache_dir / "evil.txt").exists()
    assert not (cache_dir / "jdk-25" / "bin" / "java.exe").exists()


def test_install_jdk25_zip_multi_top_dirs_not_stripped(cache_dir, tmp_path):
    # With two top-level directories the single-root guard must not strip:
    # bin/java.exe lands under the top dir, so the layout check fails.
    archive = tmp_path / "jdk-25-download.zip"
    archive.write_bytes(
        make_jdk_zip(
            {
                "jdk-25.0.1+9/bin/java.exe": b"MZ\x90",
                "other/stuff.txt": b"x\n",
            }
        )
    )
    target = cache_dir / "jdk-25"
    toolchain._extract_jdk_archive(archive, target)

    with pytest.raises(ValueError, match="did not produce"):
        toolchain._check_jdk_layout(target, ".exe")


# -- ensure_toolchain ---------------------------------------------------------------


def test_ensure_toolchain_mocked(cache_dir, mock_transport):
    base = f"https://d.defold.com/archive/{STABLE_SHA1}"
    platform_name = config.host_platform_info()["platform"]
    responses = {
        "https://d.defold.com/stable/info.json": httpx.Response(
            200, json=STABLE_INFO
        ),
        f"{base}/bob/bob.jar": httpx.Response(200, content=b"PK\x03\x04bob"),
        # The engines carry their real host magic: on Linux the cached
        # artifact is re-verified as an ELF binary on reuse.
        f"{base}/engine/{platform_name}/dmengine": httpx.Response(
            200, content=b"\x7fELFengine"
        ),
        f"{base}/engine/{platform_name}/dmengine_headless": httpx.Response(
            200, content=b"\x7fELFheadless"
        ),
        f"{base}/engine/share/builtins.zip": httpx.Response(
            200, content=make_zip({"manifests/android/android.manifest": b"m\n"})
        ),
        f"{base}/engine/share/ref-doc.zip": httpx.Response(
            200, content=make_zip({"doc/index.html": b"<html>\n"})
        ),
    }
    client, _ = mock_transport(responses)

    result = toolchain.ensure_toolchain(
        "stable", client=client, include_editor_binaries=True
    )

    engine_dir = cache_dir / "defold-1.14.1"
    assert (engine_dir / "bob.jar").read_bytes() == b"PK\x03\x04bob"
    assert (engine_dir / "dmengine").read_bytes() == b"\x7fELFengine"
    assert (engine_dir / "dmengine_headless").read_bytes() == b"\x7fELFheadless"
    assert (cache_dir / "builtins.zip").exists()
    assert (cache_dir / "ref-doc.zip").exists()
    if os.name == "posix":
        assert stat.S_IMODE((engine_dir / "dmengine").stat().st_mode) == 0o755
        assert stat.S_IMODE((engine_dir / "dmengine_headless").stat().st_mode) == 0o755

    assert result["version"] == "1.14.1"
    assert result["sha1"] == STABLE_SHA1
    assert result["bob_jar"] == str(engine_dir / "bob.jar")
    assert result["dmengine"] == str(engine_dir / "dmengine")
    assert result["dmengine_headless"] == str(engine_dir / "dmengine_headless")
    assert result["builtins_zip"] == str(cache_dir / "builtins.zip")
    assert result["ref_doc_zip"] == str(cache_dir / "ref-doc.zip")


def test_ensure_toolchain_redownloads_a_corrupt_cached_engine(
    cache_dir, mock_transport
):
    # bob.jar/builtins/ref-doc re-verify their magic on reuse; an engine
    # that is not the host's binary format must not be trusted either.
    platform_info = config.host_platform_info()
    if platform_info["os"] != "Linux":
        pytest.skip("engine magic is only checked on Linux")
    base = f"https://d.defold.com/archive/{STABLE_SHA1}"
    platform_name = platform_info["platform"]
    headless_url = f"{base}/engine/{platform_name}/dmengine_headless"
    responses = {
        "https://d.defold.com/stable/info.json": httpx.Response(
            200, json=STABLE_INFO
        ),
        f"{base}/bob/bob.jar": httpx.Response(200, content=b"PK\x03\x04bob"),
        headless_url: httpx.Response(200, content=b"\x7fELFheadless"),
        f"{base}/engine/share/builtins.zip": httpx.Response(
            200, content=make_zip({"manifests/android/android.manifest": b"m\n"})
        ),
        f"{base}/engine/share/ref-doc.zip": httpx.Response(
            200, content=make_zip({"doc/index.html": b"<html>\n"})
        ),
    }
    client, transport = mock_transport(responses)

    # A cached engine that is not an ELF binary (a truncated download, an
    # HTML error page) must be re-fetched rather than trusted.
    engine_dir = cache_dir / "defold-1.14.1"
    engine_dir.mkdir(parents=True)
    (engine_dir / "dmengine_headless").write_bytes(b"<html>not an engine")

    toolchain.ensure_toolchain("stable", client=client)

    assert (engine_dir / "dmengine_headless").read_bytes() == b"\x7fELFheadless"
    assert headless_url in transport.calls, "the corrupt engine was not re-fetched"
