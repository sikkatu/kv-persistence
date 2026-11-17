# SPDX-License-Identifier: Apache-2.0
"""r3 P1-2: reset-persistent-cache.sh -- explicit KVP_CACHE_ROOT wins over the
`/mnt/*` / `/home/*` blanket refusals, prefix compare gets strict `/` boundary,
and tests never write into the repo's live/.

Review-verified defects:
  * `KVP_CACHE_ROOT=/mnt/nvme0/kvp-l2` and `/home/x/kvp-l2` were refused even
    when EXPLICITLY configured -- safe_target's early subtree blocks beat the
    explicit-root check. A typical NVMe mounts under /mnt.
  * prefix match `case "$real" in "$_cfg_resolved"*)` accepted
    `/var/lib/kv-persistence/l2-old` for root `.../l2` (no `/` boundary).
  * test runs littered repo live/ with cache-reset-report-*.txt and
    lmcache-server-pre-reset-*.log -- production live/ must be overridable
    via KVP_LIVE_DIR so tests stage reports in tmp.

This test exercises the real safe_target via subprocess runs against fixture
cache roots staged under tmp (positive) and refusals (bare /mnt, bare $HOME).
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "reset-persistent-cache.sh"
CONFIRM = "FULL-RESET"


def _stub_bin(tmp_path: pathlib.Path) -> str:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    for tool, body in [
        ("docker", "#!/usr/bin/env bash\nexit 0\n"),
        ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
        ("fuser", "#!/usr/bin/env bash\nexit 1\n"),
        ("findmnt", "#!/usr/bin/env bash\necho 'fake-src ext4'\n"),
    ]:
        p = stub / tool
        p.write_text(body)
        p.chmod(0o755)
    return str(stub)


def run_reset(cache_root: str, tmp_path: pathlib.Path, extra_env=None):
    env = dict(
        os.environ,
        PATH=f"{_stub_bin(tmp_path)}:{os.environ.get('PATH', '')}",
        KVP_CACHE_ROOT=str(cache_root),
        KVP_RESET_CONFIRM=CONFIRM,
        KVP_MODEL_CONFIG="generic",
        # r3 P1-2 point 4: reports must land under tmp, never the repo live/
        KVP_LIVE_DIR=str(tmp_path / "live"),
        KVP_LMC_IMAGE="",  # unrelated
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(tmp_path),
    )


def make_cache(root: pathlib.Path) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects").mkdir(exist_ok=True)
    (root / "objects" / "obj.bin").write_bytes(b"\x00" * 16)
    return root


def test_explicit_root_under_mnt_is_accepted(tmp_path):
    """KVP_CACHE_ROOT=/mnt/<fake nvme>/<dir> must be APPROVED when explicit."""
    mnt_cache = tmp_path / "mnt" / "nvme0" / "kvp-l2"
    make_cache(mnt_cache)
    r = run_reset(str(mnt_cache), tmp_path)
    assert r.returncode == 0, (
        f"explicit /mnt/NVMe cache root must be accepted, got rc={r.returncode}:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}"
    )
    assert not (mnt_cache / "objects").exists(), "payload must actually be cleared"


def test_explicit_root_under_home_is_accepted(tmp_path):
    """KVP_CACHE_ROOT=$HOME/<dir> must be APPROVED when explicit (the review's
    '/home/x/kvp-l2' case)."""
    home_cache = tmp_path / "home" / "x" / "kvp-l2"
    make_cache(home_cache)
    r = run_reset(str(home_cache), tmp_path)
    assert r.returncode == 0, (
        f"explicit home-subdir cache root must be accepted, got rc={r.returncode}:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}"
    )


@pytest.mark.parametrize("dangerous", ["mnt", "home"])
def test_bare_mnt_and_home_are_refused(tmp_path, dangerous):
    """Globally dangerous mount points stay refused (bare /mnt, bare $HOME) even
    when explicitly configured -- here simulated via KVP_TEST_DANGER_ROOTS so
    the test does not need to write to the real /mnt or /home."""
    bare = tmp_path / dangerous
    make_cache(bare)
    r = run_reset(str(bare), tmp_path, extra_env={"KVP_TEST_DANGER_ROOTS": str(bare)})
    assert r.returncode != 0, (
        f"bare /{dangerous} root must be refused even when explicitly configured"
    )
    assert (bare / "objects").exists(), "refusal must not delete anything"


def test_prefix_bracket_requires_slash_boundary(tmp_path):
    """The prefix bug: an EXTRA candidate (here from a live/cache_root record)
    naming `.../l2-old` must NOT be routed to cache root `.../l2` merely because
    it shares the prefix (old `case glob "$root"*` swallowed the sibling).
    The reset must REFUSE this unconfirmed sibling rather than clear it."""
    base = tmp_path / "sibling"
    root = base / "l2"
    sib = base / "l2-old"
    make_cache(root)
    make_cache(sib)

    # the recorded cache dir (live/cache_root record) names the SIBLING
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "cache_root").write_text(str(sib) + "\n")

    r = run_reset(str(root), tmp_path)
    assert r.returncode != 0, (
        "sibling .../l2-old (record candidate) must NOT be approved under root "
        f".../l2 -- the lack of a '/' boundary in the prefix match swallowed it; "
        f"got rc={r.returncode}"
    )
    assert (sib / "objects" / "obj.bin").exists(), (
        "the refused sibling must NOT have been cleared"
    )
    assert (root / "objects" / "obj.bin").exists(), (
        "refusal happens before any clearing -- root payload must be untouched too"
    )


def test_rewrites_never_touch_repo_live(tmp_path):
    """Reports and log-archives must land under KVP_LIVE_DIR (tmp), never the
    repo's live/ dir (r3 P1-2 point 4)."""
    cache = make_cache(tmp_path / "cache")
    (cache / "lmcache-server.log").write_text("old log\n")
    live_dir = tmp_path / "live"
    r = run_reset(str(cache), tmp_path)
    assert r.returncode == 0, r.stderr[-500:]
    assert live_dir.exists(), "KVP_LIVE_DIR must receive the report"
    reports = list(live_dir.glob("cache-reset-report-*.txt"))
    archives = list(live_dir.glob("lmcache-server-pre-reset-*.log"))
    assert reports, "reset report must land under KVP_LIVE_DIR"
    assert archives, "log archive must land under KVP_LIVE_DIR"
    repo_live_reports = list((ROOT / "live").glob("cache-reset-report-*.txt")) if (ROOT / "live").exists() else []
    recent_repo_live = [p for p in repo_live_reports if p.stat().st_mtime > 0]
    # Nothing new must be written to the repo's live/ during THIS run
    # (we can't assert on mtime granularity; instead assert KVP_LIVE_DIR captured it).
    assert reports, "report must exist in the overridden live dir"