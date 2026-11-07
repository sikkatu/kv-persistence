"""Round 55: KVP_CACHE_DEVICE auto-detection (VM + LUKS: no manual guessing).

User: "какое устройство сразу тогда и пусть подставляет автоматом, я сам тоже
не знаю. у меня там все через вм и еще на нем зашифрованный раздел перед
бутом". The resolver must handle /dev/mapper/* (LUKS), /dev/vda* (VM) and
/dev/nvme*p* (bare metal) via findmnt, remember the choice in
live/cache_device, and prefer an explicit pin.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- shared helper
def test_device_resolver_defined():
    t = _read("env.sh")
    assert "_kvp_resolve_cache_device(){" in t
    assert "findmnt" in t, "the resolver must use findmnt (handles VM/LUKS/NVMe)"
    assert "readlink -f" in t, (
        "mapper symlinks (/dev/mapper/cryptroot) must be resolved to a canonical node")


def test_test_recovery_uses_the_resolver():
    t = _read("test-recovery.sh")
    assert '_kvp_resolve_cache_device' in t
    assert "live/cache_device" in t, "the detected device must be remembered"
    # the auto-detect must run BEFORE disk_read_bytes uses it
    i_detect = t.index("_kvp_resolve_cache_device")
    i_use = t.index("disk_read_bytes(){")
    assert i_detect < i_use, (
        "auto-detection must happen before disk_read_bytes (it needs the device)")


def test_preflight_uses_the_resolver():
    t = _read("preflight.sh")
    assert '_kvp_resolve_cache_device' in t


def test_resolver_prefers_explicit_pin(tmp_path):
    env_sh = _read("env.sh")
    start = env_sh.index("_kvp_resolve_cache_device(){")
    end = env_sh.index("\n_kvp_load_env", start)
    helper = env_sh[start:end]
    # also need _kvp_repo_root-ish logic: the helper falls back to BASH_SOURCE
    code = (f'KVP_CACHE_DEVICE=/dev/pinned9; {helper}\n'
            f'printf "%s" "$(_kvp_resolve_cache_device /nonexistent)"\n')
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert "/dev/pinned9" in r.stdout, (
        "an explicit KVP_CACHE_DEVICE pin must win over auto-detection")


def test_resolver_executes_without_pin(tmp_path):
    """Without a pin the helper must not crash -- it prints nothing when the
    directory has no findmnt entry (e.g. inside a test sandbox)."""
    env_sh = _read("env.sh")
    start = env_sh.index("_kvp_resolve_cache_device(){")
    end = env_sh.index("\n_kvp_load_env", start)
    helper = env_sh[start:end]
    code = helper + f'\nprintf "OUT:%s" "$(_kvp_resolve_cache_device "{tmp_path}")"\n'
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "KVP_CACHE_DEVICE": ""})
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("OUT:"), (r.stdout, r.stderr)


# ---------------------------------------------------------------- docs honesty
def test_final_report_no_stale_needs_testing_rows():
    t = (ROOT / "docs" / "final-report.md").read_text()
    for line in t.splitlines():
        # rows the hardware run proved must not claim NEEDS TESTING anymore
        if "MP server + `LMCacheMPConnector`" in line:
            assert "NEEDS TESTING" not in line, line
        if "version-pinned" in line or "lmcache==0.5.6" in line:
            pass  # paragraph text, checked below
    assert "host venv 0.5.6 + image 0.5.6 = version match" in t, (
        "the version-pin row must be VERIFIED")