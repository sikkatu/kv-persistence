# SPDX-License-Identifier: Apache-2.0
"""r4 P0-2: reset-persistent-cache.sh must refuse to wipe an EXPLICITLY-given
cache root unless it proves itself a cache dir (reproduced: KVP_CACHE_ROOT=$HOME
wipes the home directory). Contract:
  * explicit root accepted ONLY with .kvp-cache-root-marker or with all
    top-level entries from the known LMCache/kvpersist set (unknown entry ->
    refusal listing the entries + marker hint);
  * ALWAYS refused: $HOME, repo/recipes/models ancestors, .git at depth <= 2;
  * refusal deletes nothing; /mnt/<x>/<dir> marked or known-set is accepted.
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


def make_cache(root: pathlib.Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects").mkdir(exist_ok=True)
    (root / "objects" / "obj.bin").write_bytes(b"\x00" * 16)


def test_home_dir_refused_even_with_known_entries(tmp_path):
    """KVP_CACHE_ROOT=$HOME (HOME -> tmp dir NOT in the exact danger list) must
    be refused -- a cache.env typo must never wipe the home directory."""
    home = tmp_path / "myhome"
    home.mkdir()
    (home / "Documents").mkdir()
    (home / "Documents" / "thesis.txt").write_text("important\n")
    (home / ".bashrc").write_text("alias ll\n")
    r = run_reset(home, tmp_path, extra_env={"HOME": str(home)})
    assert r.returncode != 0, (
        f"$HOME as cache root must be refused, rc={r.returncode}:\n{r.stdout[-600:]}\n{r.stderr[-600:]}"
    )
    assert (home / "Documents" / "thesis.txt").exists(), "refusal must not delete"
    assert (home / ".bashrc").exists(), "refusal must not delete"


def test_repo_ancestor_refused_even_with_marker(tmp_path):
    """An ANCESTOR of a repo checkout (the fake-home repro had
    sikkatu/kv-persistence/.git inside) must be refused even with the marker."""
    anc = tmp_path / "anc"
    repo = anc / "sikkatu" / "kv-persistence"
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("code\n")
    (anc / ".kvp-cache-root-marker").write_text("kvp cache root\n")
    r = run_reset(anc, tmp_path)
    assert r.returncode != 0, f"repo-ancestor dir must be refused, rc={r.returncode}"
    assert (repo / "README.md").exists()


def test_git_within_two_levels_refused(tmp_path):
    """.git anywhere at nesting depth <= 2 inside the target -> refuse."""
    root = tmp_path / "gitful"
    (root / "sub" / "proj" / ".git").mkdir(parents=True)   # depth 2
    (root / "sub" / "proj" / "file.txt").write_text("x\n")
    r = run_reset(root, tmp_path)
    assert r.returncode != 0
    assert (root / "sub" / "proj" / "file.txt").exists()


def test_unknown_entries_without_marker_refused_with_listing(tmp_path):
    """Unknown top-level entries + no marker -> refuse, the refusal must list
    the entries and hint at creating the marker after inspection."""
    root = tmp_path / "mixed"
    make_cache(root)                      # known entries
    (root / "notes.txt").write_text("?\n")   # UNKNOWN entry
    r = run_reset(root, tmp_path)
    assert r.returncode != 0, f"unknown entries without marker must be refused, rc={r.returncode}"
    out = r.stdout + r.stderr
    assert "notes.txt" in out, f"the refusal must name the unknown entry:\n{out}"
    assert "kvp-cache-root-marker" in out, "the refusal must hint at the marker"
    assert (root / "notes.txt").exists()
    assert (root / "objects" / "obj.bin").exists()


def test_marked_root_under_mnt_accepted(tmp_path):
    mnt_cache = tmp_path / "mnt" / "nvme0" / "kvp-l2"
    make_cache(mnt_cache)
    (mnt_cache / ".kvp-cache-root-marker").write_text("kvp cache root\n")
    (mnt_cache / "weird-notes.txt").write_text("unknown but marked\n")
    r = run_reset(str(mnt_cache), tmp_path)
    assert r.returncode == 0, (
        f"marked cache root under /mnt/<x> must be accepted:\n{r.stdout[-600:]}\n{r.stderr[-600:]}"
    )
    assert not (mnt_cache / "objects").exists(), "payload must actually be cleared"


def test_known_entry_set_without_marker_accepted(tmp_path):
    """All top-level entries from the known LMCache/kvpersist set -> accepted
    without any marker."""
    root = tmp_path / "mnt" / "nvme1" / "kvp-l2-known"
    root.mkdir(parents=True)
    (root / "lmcache-server.log").write_text("log\n")
    (root / "lmcache-server.pid").write_text("1234\n")
    (root / "l2-adapter.rendered.json").write_text("{}\n")
    (root / "engine-identity.env").write_text("X=1\n")
    (root / "crash-20250101T000000Z").mkdir()
    (root / "objects").mkdir()
    (root / "objects" / "obj.bin").write_bytes(b"\x00" * 16)
    (root / "kvp-mymodel").mkdir()
    (root / ("mymodel_00000000_1_" + "a" * 64 + ".bin")).write_bytes(b"\x00" * 8)
    (root / ("tmp-sale.bin.tmp." + "b" * 32)).write_bytes(b"\x00" * 4)
    (root / ".kvp-active-writes").mkdir()
    r = run_reset(str(root), tmp_path)
    assert r.returncode == 0, (
        f"known LMCache/kvpersist entry set must be accepted:\n{r.stdout[-600:]}\n{r.stderr[-600:]}"
    )
    assert not (root / "objects").exists(), "payload must actually be cleared"
