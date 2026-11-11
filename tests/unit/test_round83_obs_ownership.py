"""Round 83 behavioural tests (replacing the old substring-only test that
could NOT catch an inverted comparison -- the reviewer flipped
[ "$_obs_wd" = "$_obs_dir" ] to != and all 3 text checks stayed green).

Three real behaviours are verified here by EXECUTION, not by grep:

1. tests/obs-ownership-stand.sh (the reviewer's stand, vendored) -- runs
   scripts/stop-all.sh against a stub `docker` (real docker is never called
   and nothing is stopped) and checks 7 scenarios:
     A  our stack (label == this repo), no marker   -> stop
     B  FOREIGN label, no marker                    -> leave
     C  foreign + KVP_STOP_OBS=1 (force)            -> stop
     D  nothing running                             -> leave
     E  grafana down, prometheus up, KVP_STOP_OBS=1 -> stop
     F  label path through a SYMLINK to this repo   -> stop
     G  stale marker + label proves FOREIGN         -> leave

2. the live/model_dir "remember" block of start-all.sh is EXTRACTED between
   the -- MODEL_DIR remember: BEGIN/END -- markers and executed in a
   sandbox: it must write live/model_dir on the first run and keep it
   consistent after (round 83 review: the file was read by
   _kvp_resolve_model_dir but nothing ever wrote it, while the wiki
   promised "set on first launch").

3. the obs-ownership marker block of start-all.sh is EXTRACTED between the
   -- obs ownership marker: BEGIN/END -- markers and executed with a stub
   docker (the stand covers stop-all only): the marker must be written for
   our stack, NOT for a foreign label (even with a stale marker present),
   and must treat a symlink to our own repo as ours.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

KV = Path(__file__).resolve().parents[2]
TESTS = KV / "tests"
STAND = TESTS / "obs-ownership-stand.sh"
START_ALL = KV / "scripts" / "start-all.sh"


# ---------------------------------------------------------------- stand
def test_obs_ownership_stand_all_scenarios_pass():
    assert STAND.is_file(), "tests/obs-ownership-stand.sh is missing"
    r = subprocess.run(
        ["bash", str(STAND), str(KV)],
        capture_output=True, text=True, timeout=120,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, "stand failed:\n" + out
    assert "ALL OK" in out, "unexpected stand output:\n" + out


# ------------------------------------------------- live/model_dir write
def _extract_block(begin: str, end: str) -> str:
    t = START_ALL.read_text()
    b = t.index(begin)
    e = t.index(end)
    block = t[t.index("\n", b) + 1: e]
    return "\n".join(
        l for l in block.splitlines()
        if end not in l
    )


def _remember_block() -> str:
    return _extract_block("-- MODEL_DIR remember: BEGIN",
                          "-- MODEL_DIR remember: END --")


def test_model_dir_remember_block_writes_file(tmp_path):
    """EXECUTE the extracted block: a first run must create live/model_dir
    containing MODEL_DIR; re-running keeps it consistent; a changed
    MODEL_DIR updates the file."""
    block = _remember_block()
    assert block.strip(), "MODEL_DIR remember block is empty"
    assert "live/model_dir" in block

    root = tmp_path
    model = tmp_path / "model"
    model.mkdir()
    script = (
        'set -u\n'
        'ROOT="' + str(root) + '"\n'
        'mkdir -p "$ROOT"\n'
        'MODEL_DIR="' + str(model) + '"\n'
        + block + '\n'
    )

    # 1st run: file is written
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    f = root / "live" / "model_dir"
    assert f.is_file(), "live/model_dir must be written on first launch"
    assert f.read_text().strip() == str(model)

    # re-run with the SAME MODEL_DIR: idempotent, still consistent
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f.read_text().strip() == str(model), (
        "an unchanged MODEL_DIR must keep live/model_dir pointing at it")

    # a DIFFERENT MODEL_DIR updates the file
    other = tmp_path / "other"
    other.mkdir()
    script2 = script.replace('MODEL_DIR="' + str(model) + '"',
                             'MODEL_DIR="' + str(other) + '"')
    r = subprocess.run(["bash", "-c", script2], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f.read_text().strip() == str(other), (
        "a changed MODEL_DIR must update live/model_dir")


# --------------------------------------- start-all obs marker block
def _obs_marker_block() -> str:
    return _extract_block("-- obs ownership marker: BEGIN",
                          "-- obs ownership marker: END --")


def _run_obs_block(workdir: Path, wd: str, started: bool,
                   names: str) -> str:
    """Execute the extracted obs-marker block with a stub docker. Returns
    '1' if live/obs_started is written afterwards, '' otherwise."""
    block = _obs_marker_block()
    bin_d = workdir / "bin"
    bin_d.mkdir(parents=True, exist_ok=True)
    stub = bin_d / "docker"
    stub.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  ps)      printf '%s\n' " + names + " ;;\n"
        "  inspect) echo '" + wd + "' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    root = workdir / "repo"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "observability").mkdir(parents=True, exist_ok=True)
    if started:
        (root / "live").mkdir(parents=True, exist_ok=True)
        (root / "live" / "obs_started").write_text("1\n")

    script = (
        "set -u\n"
        'HERE="' + str(root / "scripts") + '"\n'
        'ROOT="' + str(root) + '"\n'
        "_obs_all=1\n"
        + block + "\n"
        '[ -f "$ROOT/live/obs_started" ] && echo MARKER_PRESENT || '
        "echo MARKER_ABSENT\n"
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=60,
        env={"PATH": str(bin_d) + ":/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return "1" if "MARKER_PRESENT" in r.stdout else ""


def test_start_all_marker_written_when_stack_is_ours(tmp_path):
    """label == this repo -> marker written (mirror of stand A)."""
    obs = tmp_path / "repo" / "observability"
    got = _run_obs_block(tmp_path, str(obs), False,
                         "obs_grafana obs_prometheus")
    assert got == "1", "our stack (label == this repo) must be recorded"


def test_start_all_marker_removed_when_stack_is_foreign(tmp_path):
    """foreign label -> no marker, even with a stale one (B + G)."""
    obs = tmp_path / "repo" / "observability"
    got = _run_obs_block(tmp_path, "/some/foreign/observability", True,
                         "obs_grafana")
    assert got == "", "a foreign stack must NOT leave a recorded marker"


def test_start_all_marker_survives_symlinked_repo(tmp_path):
    """label via a SYMLINK to our repo -> still ours (F)."""
    (tmp_path / "repo" / "observability").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "repo")
    got = _run_obs_block(tmp_path, str(link / "observability"), False,
                         "obs_grafana obs_prometheus")
    assert got == "1", "a symlink to our own repo must still count as ours"