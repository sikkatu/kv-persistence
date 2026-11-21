# SPDX-License-Identifier: Apache-2.0
"""r2 P0-3: reset-persistent-cache.sh safety contract.

The first version of the script had three real defects (reviewed):
1. `find "$t" -mindepth 1 -exec rm -rf` wiped lmcache-server.log which lives
   INSIDE the cache root -- the spec forbids deleting diagnostic logs.
2. On a symlinked cache root `find "$symlink" -mindepth 1` matched nothing,
   yet the report claimed "cleared".
3. No mount-filesystem check, no host-mp-server detection, no report fields.

This test file pins every safety behaviour: refusal without CONFIRM, refusal
of dangerous paths, symlink escapes, log preservation, real clearing of a
symlinked root, and the leftover post-check.
"""

from __future__ import annotations

# Standard
import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "reset-persistent-cache.sh"
CONFIRM = "FULL-RESET"


def _quiet_tools(tmp_path: pathlib.Path) -> str:
    """A PATH stub dir making docker/pgrep/fuser report NOTHING is running.

    Used only for tests whose purpose is NOT the service-running refusal, so a
    busy dev box with unrelated containers cannot break them. The refusal test
    installs its own stub instead and does NOT use this one.
    """
    stub = tmp_path / "bin-noiseless"
    stub.mkdir(parents=True, exist_ok=True)
    for tool, body in (
        ("docker", "#!/usr/bin/env bash\ncase \"$1\" in ps) exit 0 ;; *) exit 0 ;; esac\n"),
        ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
        ("fuser", "#!/usr/bin/env bash\nexit 1\n"),
    ):
        p = stub / tool
        p.write_text(body)
        p.chmod(0o755)
    return str(stub)


def run_reset(cache_root: str, confirm: str | None = CONFIRM, extra_env=None,
              quiet_services: bool = True, stub_bin: str | None = None,
              tmp_path: pathlib.Path | None = None):
    """Run the reset script against <cache_root>.

    quiet_services: install PATH stubs so docker/pgrep/fuser report "nothing
    is running" -- keeps a busy dev box from breaking tests that are not about
    service refusal.
    stub_bin: explicit PATH stub dir (prepended) -- used by the service-refusal
    test to fake a running container; combined with quiet_services=False is
    not needed because stub_bin wins.
    """
    env = dict(os.environ)
    env["KVP_CACHE_ROOT"] = cache_root
    if confirm is not None:
        env["KVP_RESET_CONFIRM"] = confirm
    else:
        env.pop("KVP_RESET_CONFIRM", None)
    env["KVP_MODEL_CONFIG"] = "generic"
    # r23 D2: reports and log archives land under a tmp live dir, never the
    # repo's real live/ (the reset script honors KVP_LIVE_DIR).
    if tmp_path is not None:
        env["KVP_LIVE_DIR"] = str(tmp_path / "live")
    if stub_bin is not None:
        env["PATH"] = stub_bin + ":" + env["PATH"]
    elif quiet_services:
        base = tmp_path if tmp_path is not None else pathlib.Path(tempfile.mkdtemp(prefix="kvp-stubs-"))
        env["PATH"] = _quiet_tools(base) + ":" + env["PATH"]
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
    )


def make_cache(tmp_path: pathlib.Path, name: str = "cache") -> pathlib.Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    # r4 P0-2: start-cache.sh stamps every cache root with the marker at init;
    # the reset only accepts unmarked dirs that contain the known entry set
    # alone (covered by test_reset_explicit_root_policy.py).
    (d / ".kvp-cache-root-marker").write_text("kvp-cache-root\n")
    (d / "lmcache-server.log").write_text("old log line\n")
    (d / "lmcache-server.pid").write_text("12345\n")
    (d / "l2-adapter.rendered.json").write_text("{}")
    (d / "crash-20250101T000000Z").write_text("crash evidence")
    (d / "objects").mkdir()
    (d / "objects" / "obj1.bin").write_bytes(b"\x00" * 128)
    (d / "index.db").write_text("index")
    return d


# ---- confirmation gate -----------------------------------------------------
def test_refuses_without_confirm(tmp_path):
    d = make_cache(tmp_path)
    r = run_reset(str(d), confirm=None)
    assert r.returncode != 0
    assert "FULL-RESET" in (r.stdout + r.stderr)
    assert (d / "objects" / "obj1.bin").exists(), "nothing must be deleted without confirm"


def test_refuses_wrong_confirm_value(tmp_path):
    d = make_cache(tmp_path)
    r = run_reset(str(d), confirm="full-reset")
    assert r.returncode != 0
    assert "FULL-RESET" in (r.stdout + r.stderr)


# ---- dangerous path refusal ------------------------------------------------
def _bad_targets(tmp_path):
    home = str(pathlib.Path.home())
    # NOTE: a path explicitly configured as KVP_CACHE_ROOT is BY DEFINITION a
    # confirmed dedicated cache root -- such a path is legitimate to clear and
    # must NOT be in this refusal list. Only genuinely dangerous paths (roots,
    # homes, system dirs, repos, model dirs) belong here.
    return ["/", home, "/etc", "/usr", str(ROOT), f"{home}/models",
            f"{home}/models/MiMo-V2.6-Pro-MOPD"]


def test_refuses_dangerous_paths(tmp_path):
    for bad in _bad_targets(tmp_path):
        r = run_reset(bad)
        assert r.returncode != 0, f"'{bad}' must be refused (exit 0 observed)"
        out = (r.stdout + r.stderr)
        assert "unsafe cache target refused" in out, (
            f"'{bad}' must print the safe-target refusal, got: {out[-400:]}"
        )


def test_symlink_inside_cache_is_safe_even_escaping(tmp_path):
    """A symlink entry inside the cache that points OUTSIDE the cache root must
    not leak deletion across the boundary: `rm -rf ln_link` removes only the
    link itself; the outside directory with its data must survive."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("must survive")
    d = make_cache(tmp_path)
    (d / "ln-link").symlink_to(outside)
    r = run_reset(str(d))
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    assert (outside / "precious.txt").exists(), "escaped data must survive the reset"
    assert not (d / "ln-link").exists(), "the symlink itself is cleared with the payload"


def test_refuses_while_service_running(tmp_path):
    """A live container blocks the reset until the operator stops everything:
    without --force-stop the script must refuse BEFORE deleting anything."""
    d = make_cache(tmp_path)
    stub = tmp_path / "bin-busy"
    stub.mkdir(exist_ok=True)
    (stub / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  ps) echo mimo-pro-tp4-hotsplit ;;\n"
        "esac\nexit 0\n"
    )
    (stub / "docker").chmod(0o755)
    (stub / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
    (stub / "pgrep").chmod(0o755)
    (stub / "fuser").write_text("#!/usr/bin/env bash\nexit 1\n")
    (stub / "fuser").chmod(0o755)
    r = run_reset(str(d), stub_bin=str(stub))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "still running" in out, f"service refusal message missing: {out[-400:]}"
    assert (d / "objects" / "obj1.bin").exists(), "nothing must be deleted before refusal"


# ---- positive: log pullution + real clear ----------------------------------
def test_clears_payload_but_preserves_diagnostics(tmp_path):
    d = make_cache(tmp_path)
    log_before = (d / "lmcache-server.log").read_text()
    r = run_reset(str(d), tmp_path=tmp_path)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    # diagnostics preserved
    assert (d / "lmcache-server.log").is_file(), "diagnostic log must be preserved"
    assert (d / "lmcache-server.log").read_text() == log_before, "log content must be unchanged"
    assert (d / "lmcache-server.pid").is_file()
    assert (d / "l2-adapter.rendered.json").is_file()
    # an archive copy must exist under the tmp live dir (KVP_LIVE_DIR)
    live = tmp_path / "live"
    archives = list(live.glob("lmcache-server-pre-reset-*.log"))
    assert archives, "log archive under the live dir is missing"
    # payload cleared
    assert not (d / "objects").exists(), "cache payload must be removed"
    assert not (d / "index.db").exists(), "index file must be removed"
    # crash evidence preserved
    crashes = list(d.glob("crash-*"))
    assert crashes, "crash evidence must be preserved"


def test_symlinked_cache_root_is_really_cleared(tmp_path):
    real = tmp_path / "real-nvme"
    real.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(real, target_is_directory=True)
    (real / "objects").mkdir()
    (real / "objects" / "obj.bin").write_bytes(b"\xff" * 16)
    (real / "lmcache-server.log").write_text("diag")
    r = run_reset(str(link))
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    assert not (real / "objects").exists(), (
        "a symlinked cache root must be really cleared (old version silently kept everything)"
    )
    assert (real / "lmcache-server.log").is_file(), "diagnostics preserved even through symlink"


def test_report_lists_paths_and_preserved(tmp_path):
    d = make_cache(tmp_path)
    r = run_reset(str(d), tmp_path=tmp_path)
    assert r.returncode == 0
    reports = sorted((tmp_path / "live").glob("cache-reset-report-*.txt"))
    assert reports, "reset report is missing"
    text = reports[-1].read_text()
    assert "--- cleared paths ---" in text
    assert "--- preserved (spec 7: never delete) ---" in text
    assert "lmcache-server.log" in text
    assert "custom_lmcache_build" in text
    assert "new_namespace" in text
    assert "leftover_non_diagnostic_entries: 0" in text


def test_refuses_to_run_from_lifecycle_scripts():
    """start/stop/restart MUST NEVER trigger the reset -- only the explicit
    confirm path invokes it."""
    for f in ("start-cache.sh", "stop-cache.sh", "start-all.sh", "stop-all.sh"):
        src = (ROOT / "scripts" / f).read_text()
        assert "reset-persistent-cache.sh" not in src, (
            f"{f} must NOT call the destructive reset"
        )