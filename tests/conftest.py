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


@pytest.fixture(autouse=True)
def _restore_contract_live_files():
    """r23 D2: the REAL start-all.sh writes live/model_dir (Round 83),
    live/obs_started (ownership marker) and live/api_key into the REPO's live/
    BY CONTRACT -- tests exercising that contract cannot redirect those writes
    to KVP_LIVE_DIR. Save/restore exactly those three files around every test;
    the session GUARD above still catches ANY other change (new files,
    live/image, image-identity.env, cache-reset reports, ...)."""
    names = ("model_dir", "obs_started", "api_key")
    saved = {}
    for n in names:
        p = REPO_LIVE / n
        saved[n] = p.read_bytes() if p.exists() else None
    yield
    for n, content in saved.items():
        p = REPO_LIVE / n
        if content is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)


@pytest.fixture(scope="session", autouse=True)
def _kvp_live_isolation(request):
    """Point KVP_LIVE_DIR at a temp dir for the whole session and guard the
    repo's real live/ against modification (r23 D2.2 + D2.3)."""
    before = _live_state()
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