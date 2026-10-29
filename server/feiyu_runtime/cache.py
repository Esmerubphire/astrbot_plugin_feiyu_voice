"""Cache-backed integrity checks for immutable release assets.

`common.verify_package` re-hashes all 7.45 GB of the TTS package and `common.verify_repo`
re-hashes 196 source files on every single call. That is the right thing to do once per
installation, but it made every request pay tens of seconds.

The package and the pinned source tree are immutable release artefacts: the only realistic
mutation is a deploy that rewrites them. So a verified state is remembered together with
every file's size and mtime; a later check re-stats the tree (milliseconds) and falls back to
full hashing as soon as anything differs. `FEIYU_VERIFY_MODE=full` forces the old behaviour.
"""

import hashlib
import json
import os
from pathlib import Path
import sys
import time

from feiyu_runtime.common import checked_path, load, sha, validate_profile

CACHE_DIR_ENV = "FEIYU_VERIFY_CACHE_DIR"
MODE_ENV = "FEIYU_VERIFY_MODE"
FULL = "full"


def cache_dir():
    explicit = os.environ.get(CACHE_DIR_ENV)
    if explicit:
        return Path(explicit)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "FeiyuVoice" / "verify-cache"


def full_check():
    return os.environ.get(MODE_ENV, "").strip().lower() == FULL


def _state_path(key):
    return cache_dir() / (key + ".json")


def _read_state(key):
    try:
        return json.loads(_state_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(key, state):
    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _state_path(key).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # a read-only cache directory must not break inference
        print("verify cache unavailable: " + str(exc), file=sys.stderr, flush=True)


def _stats(root, names):
    out = {}
    for name in names:
        st = checked_path(root, name).stat()
        out[name] = [st.st_size, st.st_mtime_ns]
    return out


def _matches(root, expected):
    if not isinstance(expected, dict) or not expected:
        return False
    try:
        actual = _stats(root, expected)
    except OSError:
        return False
    return actual == {name: list(value) for name, value in expected.items()}


def verify_package(package):
    """Verify every recorded asset, then return the manifest (cached between runs)."""
    package = Path(package).resolve()
    records = load(package / "files_sha256.json")
    manifest_path = package / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    key = "package-" + hashlib.sha256(
        manifest_bytes + json.dumps(records, sort_keys=True).encode()
    ).hexdigest()[:32]
    state = _read_state(key)
    if (
        not full_check()
        and state
        and state.get("manifest_sha256") == manifest_sha
        and set(state.get("files", {})) == set(records)
        and _matches(package, state.get("files", {}))
    ):
        return json.loads(manifest_bytes.decode("utf-8-sig"))
    started = time.perf_counter()
    for name, record in records.items():
        path = checked_path(package, name)
        if "bytes" in record and path.stat().st_size != record["bytes"]:
            raise ValueError("Package asset size changed: " + name)
        if sha(path) != record["sha256"]:
            raise ValueError("Package integrity check failed: " + name)
    _write_state(key, dict(manifest_sha256=manifest_sha, files=_stats(package, records),
                           verified_at=time.time(), verify_seconds=time.perf_counter() - started))
    return json.loads(manifest_bytes.decode("utf-8-sig"))


def verify_repo(profile):
    """Verify the pinned source tree, then return its path (cached between runs)."""
    import subprocess

    validate_profile(profile)
    repo = Path(profile["repo"]).resolve()
    actual = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if actual != profile["commit"]:
        raise ValueError("Runtime source commit changed")
    names = profile["source_sha256"]
    key = "source-" + hashlib.sha256(
        (actual + json.dumps(names, sort_keys=True)).encode()
    ).hexdigest()[:32]
    state = _read_state(key)
    if (
        not full_check()
        and state
        and state.get("commit") == actual
        and set(state.get("files", {})) == set(names)
        and _matches(repo, state.get("files", {}))
    ):
        return repo
    started = time.perf_counter()
    for name, digest in names.items():
        if sha(checked_path(repo, name)) != digest:
            raise ValueError("Runtime source changed: " + name)
    _write_state(key, dict(commit=actual, files=_stats(repo, names),
                           verified_at=time.time(), verify_seconds=time.perf_counter() - started))
    return repo


def describe():
    return dict(mode=FULL if full_check() else "fast", cache_dir=str(cache_dir()))
