"""Toolchain resolution, artifact download and Java management.

Defold distributes artifacts by engine build SHA1. Channel specs
(``stable``/``beta``/``alpha``) and pinned dotted versions both resolve
through ``https://d.defold.com/{spec}/info.json``; a 404 for a pinned spec
falls back to version-keyed GitHub release URLs (``sha1`` is then ``None``).
Resolution results are cached with a one-hour TTL in
``<cache>/versions.json``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path

import httpx

from . import config

# Dotted version specs such as "1.14.1".
_VERSION_SPEC_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Channel specs served by d.defold.com.
_CHANNEL_SPECS = frozenset({"stable", "beta", "alpha"})

# Resolution cache time-to-live, in seconds.
_VERSION_TTL_SECONDS = 3600

# Matches both `openjdk version "21.0.12.1"` and `java version "25"` forms.
_JAVA_VERSION_RE = re.compile(r'version "(\d+)(?:\.\d+)*')


def _client_or_default(client: httpx.Client | None) -> httpx.Client:
    """Return the injected client, or a default one (caller must close)."""
    return client if client is not None else httpx.Client()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (``.tmp`` then ``os.replace``)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# -- version / channel resolution ---------------------------------------------


def _load_version_cache(cache_dir: Path) -> dict:
    """Load ``versions.json``; a missing or malformed file yields ``{}``."""
    path = cache_dir / "versions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_version_cache(cache_dir: Path, cache: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(cache_dir / "versions.json", json.dumps(cache, indent=2))


def resolve_version(
    spec: str = "stable",
    *,
    client: httpx.Client | None = None,
    force: bool = False,
) -> dict:
    """Resolve a channel or pinned version spec to ``{"version", "sha1"}``.

    Channel specs hit ``https://d.defold.com/{channel}/info.json``; dotted
    version specs hit ``https://d.defold.com/{version}/info.json``. Results
    are cached in ``<cache>/versions.json`` keyed by spec with a one-hour
    TTL; ``force=True`` bypasses the cache. A 404 for a pinned spec falls
    back to version-keyed mode (``sha1`` is ``None``) and is not cached.
    """
    if spec not in _CHANNEL_SPECS and not _VERSION_SPEC_RE.match(spec):
        raise ValueError(
            f"Unrecognised version spec {spec!r}: expected a channel "
            f"({', '.join(sorted(_CHANNEL_SPECS))}) or a dotted version "
            "such as 1.14.1."
        )

    cache_dir = config.get_cache_dir()
    if not force:
        entry = _load_version_cache(cache_dir).get(spec)
        if isinstance(entry, dict) and "version" in entry:
            fetched_at = entry.get("fetched_at")
            if (
                isinstance(fetched_at, (int, float))
                and time.time() - fetched_at < _VERSION_TTL_SECONDS
            ):
                return {"version": entry["version"], "sha1": entry.get("sha1")}

    url = f"{config.D_DEFOLD_BASE}/{spec}/info.json"
    owned = client is None
    http = _client_or_default(client)
    try:
        response = http.get(url)
    finally:
        if owned:
            http.close()
    if response.status_code != 200:
        if _VERSION_SPEC_RE.match(spec) and response.status_code == 404:
            # Pinned spec unknown to d.defold.com: GitHub-release mode.
            return {"version": spec, "sha1": None}
        raise ValueError(
            f"Failed to resolve {spec!r}: {url} returned "
            f"HTTP {response.status_code}."
        )

    info = response.json()
    version = info.get("version")
    if version is None:
        raise ValueError(f"No 'version' field in info.json at {url}.")
    result = {"version": str(version), "sha1": info.get("sha1")}
    cache = _load_version_cache(cache_dir)
    cache[spec] = {**result, "fetched_at": time.time()}
    _save_version_cache(cache_dir, cache)
    return result


# -- Java discovery -------------------------------------------------------------


def _parse_java_major(stderr: str) -> int | None:
    match = _JAVA_VERSION_RE.search(stderr)
    if not match:
        return None
    return int(match.group(1))


def _probe_java(java_path: Path, source: str) -> dict:
    """Run ``java -version`` and report the major version (never raises)."""
    try:
        proc = subprocess.run(
            [str(java_path), "-version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "ok": False,
            "major": None,
            "java_path": str(java_path),
            "source": source,
            "diagnostic": f"Could not execute {java_path}.",
        }
    major = _parse_java_major(proc.stderr)
    return {
        "ok": major is not None and major >= config.MIN_BOB_JAVA_MAJOR,
        "major": major,
        "java_path": str(java_path),
        "source": source,
        "diagnostic": None,
    }


def _diagnostic_for(major: int | None) -> str:
    found = f" (found major {major})" if major is not None else ""
    return (
        f"Java {config.MIN_BOB_JAVA_MAJOR}+ is required for bob.jar but the "
        f"best available Java does not qualify{found}. Install a portable "
        f"JDK via defold_setup_toolchain(install_jdk25=True), or use the "
        f"Editor /bob route which manages its own Java."
    )


def check_java() -> dict:
    """Locate and probe the best available Java.

    Discovery order: portable ``<cache>/jdk-25/bin/java`` -> ``JAVA_HOME``
    -> ``shutil.which("java")``. Every existing candidate is probed and
    the first one whose major version qualifies is returned (matching
    :func:`java_executable`), so a broken portable JDK cannot shadow a
    good ``JAVA_HOME`` Java. When no candidate qualifies the first
    existing failure is reported. Never raises for a missing Java;
    returns ``ok`` False instead.
    """
    exe_suffix = ".exe" if os.name == "nt" else ""
    candidates: list[tuple[Path, str]] = []

    candidates.append(
        (config.get_cache_dir() / "jdk-25" / "bin" / f"java{exe_suffix}", "portable")
    )
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        candidates.append((Path(java_home) / "bin" / f"java{exe_suffix}", "JAVA_HOME"))
    on_path = shutil.which("java")
    if on_path:
        candidates.append((Path(on_path), "PATH"))

    first_failure: dict | None = None
    for java_path, source in candidates:
        if java_path.is_file():
            result = _probe_java(java_path, source)
            if result["ok"]:
                return result
            if first_failure is None:
                if result["major"] is not None:
                    result["diagnostic"] = _diagnostic_for(result["major"])
                first_failure = result
    if first_failure is not None:
        return first_failure

    return {
        "ok": False,
        "major": None,
        "java_path": None,
        "source": None,
        "diagnostic": (
            "No Java found. Set JAVA_HOME, run "
            "defold_setup_toolchain(install_jdk25=True) to install a "
            "portable JDK, or use the Editor /bob route."
        ),
    }


def java_executable() -> Path | None:
    """Return the best Java executable at or above the required major.

    The portable JDK wins when present and qualifying; otherwise a
    ``JAVA_HOME``/``PATH`` Java qualifies if its major is sufficient.
    Returns ``None`` when nothing qualifies.
    """
    exe_suffix = ".exe" if os.name == "nt" else ""
    portable = config.get_cache_dir() / "jdk-25" / "bin" / f"java{exe_suffix}"
    if portable.is_file():
        probe = _probe_java(portable, "portable")
        if probe["ok"]:
            return portable
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        candidate = Path(java_home) / "bin" / f"java{exe_suffix}"
        if candidate.is_file() and _probe_java(candidate, "JAVA_HOME")["ok"]:
            return candidate
    on_path = shutil.which("java")
    if on_path and _probe_java(Path(on_path), "PATH")["ok"]:
        return Path(on_path)
    return None


# -- downloads -------------------------------------------------------------------


def download_artifact(
    url: str,
    dest: Path,
    *,
    client: httpx.Client | None = None,
    expected_magic: bytes | None = None,
) -> Path:
    """Download ``url`` to ``dest`` atomically (``.tmp`` then ``os.replace``).

    When ``expected_magic`` is given, the first bytes of the downloaded
    file must match it; a mismatch raises ``ValueError`` naming the
    expected and actual bytes, and the partial file is removed.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")

    owned = client is None
    http = _client_or_default(client)
    try:
        response = http.get(url)
        response.raise_for_status()
        data = response.content
    finally:
        if owned:
            http.close()

    if expected_magic is not None:
        actual = data[: len(expected_magic)]
        if actual != expected_magic:
            raise ValueError(
                f"Magic bytes mismatch for {url}: expected "
                f"{expected_magic!r}, got {actual!r}."
            )

    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


# -- JDK 25 installer ---------------------------------------------------------------


def adoptium_jdk_url() -> str:
    """Return the Adoptium binary URL for Temurin JDK 25 on this host."""
    if os.name == "nt":
        adoptium_os = "windows"
    elif platform.system() == "Darwin":
        adoptium_os = "mac"
    else:
        adoptium_os = "linux"

    machine = platform.machine()
    if machine in ("x86_64", "amd64", "AMD64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        raise ValueError(f"Unsupported machine for JDK download: {machine!r}")

    return (
        f"https://api.adoptium.net/v3/binary/latest/25/ga/{adoptium_os}/"
        f"{arch}/jdk/hotspot/normal/eclipse"
    )


def _reject_unsafe_members(names: list[str], dest: Path) -> None:
    """Raise if any archive member would escape ``dest``.

    Absolute paths, ``..`` path components, and backslash characters are
    rejected before anything is written. Backslash is a path separator on
    Windows and can smuggle traversal past the forward-slash-only check.
    """
    for name in names:
        if not name:
            continue
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            raise ValueError(
                f"Refusing to extract unsafe archive entry {name!r} "
                f"(path escapes {dest})."
            )


def _extract_jdk_archive(archive: Path, target: Path) -> None:
    """Extract a JDK archive into ``target``, stripping a single root.

    The archive's single top-level directory (e.g. ``jdk-25.0.1+9/``) is
    stripped so the JDK lands directly under ``target``; archives with
    more than one top-level directory are extracted verbatim. Member
    names are validated before anything is written (absolute paths and
    ``..`` components are rejected). Symlink members are skipped:
    Adoptium JDK layouts are all regular files.
    """
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            _reject_unsafe_members([m.filename for m in members], target)
            prefixes = {
                m.filename.split("/", 1)[0] for m in members if m.filename
            }
            prefix = next(iter(prefixes)) + "/" if len(prefixes) == 1 else ""
            dest_resolved = target.resolve()
            for member in members:
                if not member.filename.startswith(prefix):
                    continue
                out = target / member.filename[len(prefix):]
                if not out.resolve().is_relative_to(dest_resolved):
                    raise ValueError(
                        f"Refusing to extract unsafe archive entry "
                        f"{member.filename!r} (path escapes {target})."
                    )
                if member.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                elif stat.S_ISLNK(member.external_attr >> 16):
                    continue
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, out.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            _reject_unsafe_members([m.name for m in members], target)
            prefixes = {m.name.split("/", 1)[0] for m in members if m.name}
            prefix = (
                next(iter(prefixes)) + "/"
                if len(prefixes) == 1
                else ""
            )
            dest_resolved = target.resolve()
            for member in members:
                if member.name.startswith(prefix):
                    out = target / member.name[len(prefix):]
                    if not out.resolve().is_relative_to(dest_resolved):
                        raise ValueError(
                            f"Refusing to extract unsafe archive entry "
                            f"{member.name!r} (path escapes {target})."
                        )
                    if member.isdir():
                        out.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        out.parent.mkdir(parents=True, exist_ok=True)
                        extracted = tf.extractfile(member)
                        if extracted is None:
                            continue
                        with extracted as src, out.open("wb") as dst:
                            shutil.copyfileobj(src, dst)


def _check_jdk_layout(target: Path, exe_suffix: str) -> None:
    """Raise if extraction did not produce ``bin/java`` under ``target``."""
    if not (target / "bin" / f"java{exe_suffix}").is_file():
        raise ValueError(
            f"JDK extraction did not produce {target / 'bin' / f'java{exe_suffix}'}. "
            "The Adoptium archive layout may have changed."
        )


def install_jdk25(*, client: httpx.Client | None = None) -> Path:
    """Download and extract Temurin JDK 25 into ``<cache>/jdk-25``.

    Idempotent: returns immediately when ``<cache>/jdk-25/bin/java``
    already exists. Extraction, member-name validation and the single-root
    strip are performed by :func:`_extract_jdk_archive`; an archive whose
    layout does not produce ``bin/java`` fails the final check.
    """
    target = config.get_cache_dir() / "jdk-25"
    exe_suffix = ".exe" if os.name == "nt" else ""
    if (target / "bin" / f"java{exe_suffix}").is_file():
        return target

    url = adoptium_jdk_url()
    archive = config.get_cache_dir() / "jdk-25-download"
    if os.name == "nt":
        archive = archive.with_suffix(".zip")
    else:
        archive = archive.with_suffix(".tar.gz")
    try:
        download_artifact(url, archive, client=client)
        _extract_jdk_archive(archive, target)
    finally:
        archive.unlink(missing_ok=True)

    _check_jdk_layout(target, exe_suffix)
    return target


# Alias used by ensure_toolchain, whose ``install_jdk25`` boolean parameter
# shadows the module-level function name in its scope.
install_jdk25_fn = install_jdk25


# -- installed editors registry ------------------------------------------------------


def _installations_path() -> Path:
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        base = Path(local_appdata) if local_appdata else Path.home()
        return base / "Defold" / "installations.json"
    if os.uname().sysname == "Darwin":
        return (
            Path.home() / "Library" / "Application Support" / "Defold"
            / "installations.json"
        )
    xdg_state = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "Defold" / "installations.json"


def read_installations() -> list[dict]:
    """Parse the OS-correct Defold ``installations.json`` if present.

    Pure file read; a missing or malformed file yields ``[]``.
    """
    path = _installations_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


# -- orchestrator ---------------------------------------------------------------------


def ensure_toolchain(
    spec: str = "stable",
    *,
    client: httpx.Client | None = None,
    force: bool = False,
    include_editor_binaries: bool = False,
    install_jdk25: bool = False,
) -> dict:
    """Resolve ``spec`` and download the toolchain into the cache.

    Layout: ``<cache>/defold-{version}/`` holds ``bob.jar``,
    ``dmengine_headless`` and (with ``include_editor_binaries``)
    ``dmengine``; ``<cache>/builtins.zip`` and ``<cache>/ref-doc.zip`` are
    shared across versions. Engines are ``chmod 0o755`` on POSIX. When
    ``install_jdk25`` is requested and ``check_java()`` reports a failure,
    a portable Temurin JDK 25 is installed first.
    """
    resolved = resolve_version(spec, client=client, force=force)
    version = resolved["version"]
    sha1 = resolved["sha1"]
    cache_dir = config.get_cache_dir()
    engine_dir = cache_dir / f"defold-{version}"
    platform_info = config.host_platform_info()

    jdk_installed = None
    if install_jdk25 and not check_java()["ok"]:
        jdk_installed = str(install_jdk25_fn(client=client))

    def _artifact_url(archive_path: str) -> str:
        if sha1 is not None:
            return f"{config.D_DEFOLD_BASE}/archive/{sha1}/{archive_path}"
        return f"{config.GITHUB_RELEASES_BASE}/{version}/{archive_path.rsplit('/', 1)[-1]}"

    result: dict = {
        "version": version,
        "sha1": sha1,
        "engine_dir": str(engine_dir),
        "bob_jar": None,
        "dmengine": None,
        "dmengine_headless": None,
        "builtins_zip": None,
        "ref_doc_zip": None,
        "jdk_installed": jdk_installed,
    }

    bob_jar = engine_dir / "bob.jar"
    download_artifact(
        _artifact_url("bob/bob.jar"), bob_jar, client=client, expected_magic=b"PK"
    )
    result["bob_jar"] = str(bob_jar)

    headless = engine_dir / platform_info["headless_exe"]
    download_artifact(
        _artifact_url(f"engine/{platform_info['platform']}/{platform_info['headless_exe']}"),
        headless,
        client=client,
    )
    result["dmengine_headless"] = str(headless)

    if include_editor_binaries:
        engine = engine_dir / f"dmengine{platform_info['exe_suffix']}"
        download_artifact(
            _artifact_url(f"engine/{platform_info['platform']}/dmengine{platform_info['exe_suffix']}"),
            engine,
            client=client,
        )
        result["dmengine"] = str(engine)

    builtins_zip = cache_dir / "builtins.zip"
    download_artifact(
        _artifact_url("engine/share/builtins.zip"),
        builtins_zip,
        client=client,
        expected_magic=b"PK",
    )
    result["builtins_zip"] = str(builtins_zip)

    ref_doc_zip = cache_dir / "ref-doc.zip"
    download_artifact(
        _artifact_url("engine/share/ref-doc.zip"),
        ref_doc_zip,
        client=client,
        expected_magic=b"PK",
    )
    result["ref_doc_zip"] = str(ref_doc_zip)

    if os.name == "posix":
        for engine_path in filter(
            None, [result["dmengine"], result["dmengine_headless"]]
        ):
            os.chmod(engine_path, 0o755)

    return result
