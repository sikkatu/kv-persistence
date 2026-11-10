"""Round 47: GPU check position + version sync + CHANGELOG + Grafana notes.

The user's run failed because the round 46 GPU check ran BEFORE the old
engine was stopped -- it refused on the engine's own legal VRAM (78 GiB
per GPU). The check must run AFTER the old engine is removed (launcher)
and AFTER both services are stopped (test-recovery scenario B).
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- GPU check position
def test_recovery_gpu_check_runs_after_stop():
    t = _read("test-recovery.sh")
    b = t[t.index("== SCENARIO B"):]
    stop_engine = b.index('docker stop "$CONTAINER"')
    stop_cache = b.index("stop-cache.sh")
    gpu_check = b.index("_kvp_gpu_preflight 2048")
    start_cache = b.index("start-cache.sh")
    assert stop_engine < stop_cache < gpu_check < start_cache, (
        "scenario B: GPU check must run AFTER docker stop + stop-cache, "
        "BEFORE start-cache (checking before the stop refuses the engine's "
        "own legal VRAM)")


def test_launcher_gpu_check_runs_after_old_container_removed():
    # Round 80b: the recipe launcher owns the engine launch -- read ITS order
    # (the cross-file union reorders across scripts).
    rl = _T.recipe_launcher()
    assert rl is not None, "the recipe launcher must exist"
    t = rl.read_text()
    rm_old = t.index('docker rm -f "$CONTAINER"')
    gpu_check = t.index("_kvp_gpu_preflight 2048")
    run_new = t.index("docker run -d --name")
    assert rm_old < gpu_check and gpu_check < run_new, (
        "launcher: GPU check must run AFTER the old container is removed, "
        "BEFORE the new docker run")


# ---------------------------------------------------------------- version sync
def test_no_stale_053_mentions_in_docs():
    bad = []
    for p in sorted(glob.glob(str(ROOT / "docs" / "*.md"))
                    + glob.glob(str(ROOT / "README*.md"))
                    + glob.glob(str(ROOT / "observability" / "*.md"))):
        for i, line in enumerate(Path(p).read_text().splitlines(), 1):
            if "0.5.3" in line:
                bad.append("%s:%d" % (Path(p).name, i))
    assert not bad, "stale 0.5.3 mentions remain: %s" % bad


def test_requirements_pin_is_056():
    req = (ROOT / "requirements-lmcache.txt").read_text()
    assert "lmcache==0.5.6" in req
    assert "lmcache==0.5.3" not in req


# ---------------------------------------------------------------- CHANGELOG
def test_changelog_exists_in_three_languages():
    for name in ("CHANGELOG.md", "CHANGELOG.ru.md", "CHANGELOG.zh.md"):
        p = ROOT / name
        assert p.exists(), "%s missing" % name
        t = p.read_text()
        assert "0.5.6" in t, "%s must reference the pinned 0.5.6" % name


# ---------------------------------------------------------------- Grafana honesty
def test_coordinator_dashboard_annotated_for_single_server():
    import json
    p = ROOT / "observability" / "grafana" / "provisioning" / "dashboards" / "lmcache-coordinator.json"
    d = json.loads(p.read_text())
    desc = d.get("description", "")
    assert "single" in desc.lower() and "no data" in desc.lower(), (
        "the coordinator dashboard must explain that 'No data' is EXPECTED "
        "in a single-server repo")
    found = False
    for panel in d.get("panels", []):
        if panel.get("title") == "Controllers up":
            found = True
            assert "no coordinator" in (panel.get("description", "")).lower()
    assert found, "the 'Controllers up' panel must carry the explanation"


def test_lmcache_dashboard_mentions_observability_flag():
    import json
    p = ROOT / "observability" / "grafana" / "provisioning" / "dashboards" / "lmcache.json"
    d = json.loads(p.read_text())
    assert "USE_OBSERVABILITY=1" in d.get("description", ""), (
        "the LMCache dashboard must tell the operator that data needs "
        "USE_OBSERVABILITY=1")