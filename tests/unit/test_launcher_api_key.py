"""Install-report fix: run-vllm-with-persist.sh API-key contract.

Review (install report): the wiki told the operator to create live/api_key BY
HAND while the recipe launchers auto-generate it on loopback. The launcher now
follows the recipe contract:
  * loopback bind (127.0.0.1/::1) + no key -> AUTO-GENERATED into live/api_key;
  * non-loopback bind + no key -> die (a routable server must be authenticated);
  * an explicit API_KEY is always honoured.
A stub docker on PATH keeps the tests from touching a real daemon.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "run-vllm-with-persist.sh"
KEY_FILE = ROOT / "live" / "api_key"

DOCKER_STUB = """#!/usr/bin/env bash
# test stub: record and succeed -- never touches a real docker daemon
echo "docker $*" >> "${STUB_LOG:-/dev/null}"
exit 0
"""


def _base_env(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("KVP_") or k in ("API_KEY", "BIND", "IMAGE", "MODEL_DIR"):
            del env[k]
    env["PATH"] = f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["STUB_LOG"] = str(tmp_path / "stub.log")
    recipe = tmp_path / "recipe"
    recipe.mkdir(exist_ok=True)
    env["RECIPE_DIR"] = str(recipe)
    # Round 56: the launcher reads the canonical key file
    # ($HOME/.config/kvp/api_key) FIRST -- point it at the repo live/api_key
    # so these tests fully control the key source (no leak from the real
    # canonical path).
    env["KVP_API_KEY_FILE"] = str(KEY_FILE)
    env["IMAGE"] = "x@sha256:" + "a" * 64
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    env["MODEL_DIR"] = str(model)
    return env


def _preserve_key():
    """Round-31 lesson: never destroy the operator's real live/api_key."""
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    return None


def _restore_key(saved):
    if saved is None:
        if KEY_FILE.exists():
            KEY_FILE.unlink()
    else:
        KEY_FILE.write_bytes(saved)


def test_loopback_bind_auto_generates_api_key(tmp_path):
    """Loopback + no key -> the launcher generates one and saves it to
    live/api_key (mode 600), exactly like the recipe launchers. The run may
    stop later for other reasons -- the KEY FILE is the contract under test."""
    saved = _preserve_key()
    try:
        if KEY_FILE.exists():
            KEY_FILE.unlink()
        env = _base_env(tmp_path)
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        combined = r.stdout + r.stderr
        assert KEY_FILE.exists(), (
            "a loopback launch with no key must AUTO-GENERATE live/api_key:\n"
            + combined[-1500:]
        )
        key = KEY_FILE.read_text().strip()
        assert len(key) >= 32, f"generated key too short: {len(key)} chars"
        assert "auto-generated" in combined, (
            "the launcher must SAY it generated the key:\n" + combined[-1500:]
        )
        mode = stat.S_IMODE(KEY_FILE.stat().st_mode)
        assert mode == 0o600, f"live/api_key must be 0600, got {oct(mode)}"
    finally:
        _restore_key(saved)


def test_non_loopback_bind_without_key_is_refused(tmp_path):
    """A routable bind with no key must be REFUSED (fail-safe: a server on a
    routable address is always authenticated) -- and no key is generated."""
    saved = _preserve_key()
    try:
        if KEY_FILE.exists():
            KEY_FILE.unlink()
        env = _base_env(tmp_path)
        env["BIND"] = "0.0.0.0"
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode != 0, (
            "a non-loopback bind without a key must abort:\n"
            + (r.stdout + r.stderr)[-1500:]
        )
        combined = r.stdout + r.stderr
        assert "non-loopback" in combined.lower(), (
            "the refusal must name the non-loopback bind:\n" + combined[-1500:]
        )
        assert not KEY_FILE.exists(), (
            "no key may be generated for a refused non-loopback bind"
        )
    finally:
        _restore_key(saved)


def test_explicit_api_key_is_honoured(tmp_path):
    """An explicit API_KEY must be used verbatim for a non-loopback bind --
    no auto-generation, no refusal."""
    saved = _preserve_key()
    try:
        if KEY_FILE.exists():
            KEY_FILE.unlink()
        env = _base_env(tmp_path)
        env["API_KEY"] = "explicit-key-0123456789abcdef0123456789"
        env["BIND"] = "0.0.0.0"
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        combined = r.stdout + r.stderr
        assert "API_KEY required" not in combined, (
            "an explicit key must satisfy a non-loopback bind:\n" + combined[-1500:]
        )
        assert "auto-generated" not in combined, (
            "an explicit key must NOT be replaced by a generated one:\n"
            + combined[-1500:]
        )
    finally:
        _restore_key(saved)