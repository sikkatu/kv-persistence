# SPDX-License-Identifier: Apache-2.0
"""r5 P1-1: the reset allowlist must cover what the REAL LMCache adapter
writes -- `*.data` (encode_fs_filename), `*.tmp` (fallback temp suffix), and
the configured `relative_tmp_dir` sub-dir from l2-adapter.rendered.json.

Reproduced (r4): cache dir with `abc123.data` + a `.tmp` dir, no marker ->
refusal "unknown top-level entries: abc123.data .tmp". The first reset on a
REAL pre-r4 cache always hit the manual `touch` -- exactly the operation the
script exists for.
"""
from __future__ import annotations
import os
import pathlib
import subprocess

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


def run_reset(cache_root, tmp_path: pathlib.Path, extra_env=None):
    env = dict(
        os.environ,
        PATH=f"{_stub_bin(tmp_path)}:{os.environ.get('PATH', '')}",
        KVP_CACHE_ROOT=str(cache_root),
        KVP_RESET_CONFIRM=CONFIRM,
        KVP_MODEL_CONFIG="generic",
        KVP_LIVE_DIR=str(tmp_path / "live"),
        KVP_LMC_IMAGE="",
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(tmp_path),
    )


def _legacy_cache(tmp_path: pathlib.Path, with_tmp_dir_cfg: bool = True) -> pathlib.Path:
    """A cache root as the real fs adapter leaves it: `*.data` per ObjectKey,
    `*.tmp` temp leftovers and a configured relative_tmp_dir sub-dir."""
    root = tmp_path / "mnt" / "xo"
    root.mkdir(parents=True)
    (root / "abc123.data").write_bytes(b"\x01" * 16)
    (root / "def456.data").write_bytes(b"\x02" * 16)
    (root / "def456.tmp").write_bytes(b"\x03" * 8)      # fallback .tmp suffix
    if with_tmp_dir_cfg:
        (root / "l2-adapter.rendered.json").write_text(
            '{"type": "nixl_store_dynamic", "backend_params": {"file_path": "'
            + str(root) + '"}, "relative_tmp_dir": "fscache-tmp", "persist_enabled": true}'
        )
        (root / "fscache-tmp").mkdir()
        (root / "fscache-tmp" / "ghi789.data").write_bytes(b"\x04" * 4)
    return root


def test_legacy_cache_without_marker_is_accepted_and_cleared(tmp_path):
    """PRE-r4 cache (no marker): real LMCache artifacts (data/tmp/tmp-dir via
    configured relative_tmp_dir) must be accepted and cleared WITHOUT a manual
    touch."""
    root = _legacy_cache(tmp_path)
    r = run_reset(root, tmp_path)
    assert r.returncode == 0, (
        f"real LMCache file set must pass untouched by hand, rc={r.returncode}:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}"
    )
    assert not (root / "abc123.data").exists(), "payload must really be cleared"
    assert not (root / "fscache-tmp").exists(), "configured tmp dir must be cleared"


def test_foreign_file_refused_with_exact_listing(tmp_path):
    """The same real file set + ONE foreign file -> refuse; the listing must
    contain exactly the foreign file, not the LMCache artifacts."""
    root = _legacy_cache(tmp_path)
    (root / "thesis.txt").write_text("not cache\n")
    r = run_reset(root, tmp_path)
    assert r.returncode != 0, "a foreign file must refuse the reset"
    out = r.stdout + r.stderr
    # the refusal names the unknown entries after "unknown top-level entries:"
    unknown_lines = [ln for ln in out.splitlines() if "unknown top-level entries" in ln]
    assert unknown_lines, f"the refusal must list the unknown entries:\n{out}"
    listing = unknown_lines[-1]
    assert "thesis.txt" in listing, f"the listing must name thesis.txt:\n{listing}"
    for kv_name in ("abc123.data", "def456.tmp", "fscache-tmp"):
        assert kv_name not in listing, (
            f"real LMCache artifact {kv_name} must NOT appear in the "
            f"unknown listing:\n{listing}"
        )
    assert (root / "abc123.data").exists(), "refusal must delete nothing"
    assert (root / "thesis.txt").exists(), "refusal must delete nothing"


def test_unnamed_tmp_dir_is_not_allowlisted(tmp_path):
    """relative_tmp_dir is only trusted from the rendered adapter config --
    a bare unconfigured sub-dir is foreign and must refuse."""
    root = tmp_path / "mnt" / "xo2"
    root.mkdir(parents=True)
    (root / "abc123.data").write_bytes(b"\x01" * 4)
    (root / "mysterious-tmp").mkdir()
    (root / "mysterious-tmp" / "x.data").write_bytes(b"")
    r = run_reset(root, tmp_path)
    assert r.returncode != 0, "an unconfigured tmp dir must NOT be allowlisted"
    assert "mysterious-tmp" in (r.stdout + r.stderr)