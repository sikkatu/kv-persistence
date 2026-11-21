# SPDX-License-Identifier: Apache-2.0
"""Shared test fixtures (r23 D2): keep the repo's real live/ untouched.

Facts (review, HEAD 2600f09): several test files wrote into the REPO's real
live/ (live/image, live/image-identity.env, api_key, ...). On a clean clone
(live/ is gitignored) two r22 tests then failed ("no IMAGE set and no
live/image") -- they only passed because earlier tests had polluted the
repository tree. On a machine with a REAL build the test run would overwrite
live/image with a fake digest and the next start-all.sh would launch a fake
image.

Contract (r23 D2):
1. every helper that needs an image writes it under a TMP KVP_LIVE_DIR;
2. an autouse session fixture points KVP_LIVE_DIR at a temp dir and any test
   that deliberately touches the repo live/ restores it;
3. a session GUARD records the state (hashes) of the repo live/ before the
   run and FAILS at the end of the session if anything changed.
"""
from __future__ import annotations
import hashlib
import os
import pathlib
import shutil
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_LIVE = ROOT / "live"


def _live_files() -> dict:
    """Snapshot of the repo live/: rel-path -> bytes (dirs listed with '/')."""
    if not REPO_LIVE.is_dir():
        return {}
    snap = {}
    for p in sorted(REPO_LIVE.rglob("*")):
        rel = str(p.relative_to(REPO_LIVE))
        if p.is_dir():
            snap[rel + "/"] = None
        else:
            snap[rel] = p.read_bytes()
    return snap


def _live_state() -> dict:
    """Hash every file under the repo live/ (or 'ABSENT' when it is gone)."""
    if not REPO_LIVE.is_dir():
        return {"__dir__": "ABSENT"}
    state = {}
    for p in sorted(REPO_LIVE.rglob("*")):
        rel = str(p.relative_to(REPO_LIVE))
        if p.is_dir():
            state[rel + "/"] = "DIR"
        else:
            state[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return state


# The session-start snapshot of the repo live/ -- every test is restored to
# exactly this state on teardown (r23 D2.2).
_SESSION_LIVE_SNAPSHOT: dict = {}


@pytest.fixture(scope="session", autouse=True)
def _kvp_live_isolation(request):
    """Point KVP_LIVE_DIR at a temp dir for the whole session and guard the
    repo's real live/ against modification (r23 D2.2 + D2.3)."""
    global _SESSION_LIVE_SNAPSHOT
    before = _live_state()
    _SESSION_LIVE_SNAPSHOT = _live_files()
    tmp_live = pathlib.Path(tempfile.mkdtemp(prefix="kvp-test-live-"))
    os.environ["KVP_LIVE_DIR"] = str(tmp_live)
    os.environ["LIVE_DIR"] = str(tmp_live)
    yield
    os.environ.pop("KVP_LIVE_DIR", None)
    os.environ.pop("LIVE_DIR", None)
    shutil.rmtree(tmp_live, ignore_errors=True)
    after = _live_state()
    if after != before:
        changed = sorted(
            set(after.items()) ^ set(before.items()))
        pytest.fail(
            "r23 D2 GUARD: the repo's real live/ was modified by the test run "
            "(tests must use the isolated KVP_LIVE_DIR, never the repo live/). "
            f"Changed entries: {[c[0] for c in changed][:20]}")


@pytest.fixture(autouse=True)
def _restore_repo_live():
    """r23 D2.2: the REAL launchers write live/model_dir, live/obs_started,
    live/api_key, live/engine_container, live/obs-relay/, live/.engine-env,
    live/image-identity.env, live/kvp-effective-identity.env, cache-reset
    reports, ... into the REPO's live/ BY CONTRACT (start-all.sh records them
    so stop-engine.sh / stop-all.sh find them). Tests exercising that
    contract cannot redirect those writes to KVP_LIVE_DIR -- so restore the
    whole repo live/ to the session snapshot around every test. The session
    GUARD above stays the last line of defence: it fails the run if anything
    is still different after all restores."""
    yield
    snap = _SESSION_LIVE_SNAPSHOT
    # remove entries that are not in the snapshot
    if REPO_LIVE.is_dir():
        for p in sorted(REPO_LIVE.rglob("*"), reverse=True):
            rel = str(p.relative_to(REPO_LIVE))
            key = rel + "/" if p.is_dir() else rel
            if key not in snap:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
    # restore every snapshot entry (missing or changed)
    for rel, content in snap.items():
        if rel.endswith("/"):
            (REPO_LIVE / rel).mkdir(parents=True, exist_ok=True)
            continue
        p = REPO_LIVE / rel
        if not p.exists() or p.read_bytes() != content:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
    # r23 D2 clean-clone: when the session started with NO live/ at all (fresh
    # clone -- live/ is gitignored) the contract writers leave an EMPTY dir
    # behind (mkdir -p). Remove it so the state matches "ABSENT" again.
    if not snap and REPO_LIVE.is_dir():
        shutil.rmtree(REPO_LIVE, ignore_errors=True)