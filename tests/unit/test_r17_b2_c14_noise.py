# SPDX-License-Identifier: Apache-2.0
"""r17 B2.4: the end-to-end r12 harness must mirror the real build's freeze
churn -- a positive control run adds pytest/lmcache/build-deps to the freeze
and MUST pass C14, a mutation run changes the ENGINE and must be refused.

The r16 freeze fixtures contained only engine packages, so the dropped
engine filter (r17 B2) was invisible. The harness pip shim is now stateful
like the real build: C7 replaces lmcache, C9 adds pytest+pluggy+iniconfig,
C4 adds setuptools-scm -- and only the mutate_on cases touch numpy.

This test drives the REAL whole _incontainer_build.sh through
test_r12_c14_after_tests._run and asserts:
  (a) positive control (mutate_on="") passes and the engine identity OK line
      appears -- proof the added pytest/lmcache noise does NOT refuse;
  (b) an engine mutation (mutate_on="pytest") is refused.
Mutation: dropping the engine filter in the C14 block -> (a) RED (the added
pytest/lmcache noise refuses the build).
"""
from __future__ import annotations
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_r12_c14_after_tests import _run  # noqa: E402  (shared harness)


def test_harness_positive_control_adds_noise_and_passes(tmp_path):
    """(a) the whole harness run adds pytest/lmcache noise to the freeze and
    STILL passes C14 (engine untouched)."""
    r = _run(tmp_path, skip_tests="0", mutate_on="")
    assert r.returncode == 0, (
        f"positive control must pass (rc={r.returncode})\n"
        f"stdout:\n{r.stdout[-1500:]}\nstderr:\n{r.stderr[-1500:]}")
    assert "engine identity OK" in r.stdout, r.stdout[-800:]
    # proof the freeze churn really happened (the stateful pip shim appended
    # it like the real build does) -- checked on the final state file
    import json
    state = json.loads((tmp_path / "freeze-state.json").read_text())
    extra = state["extra"]
    assert any(x.startswith("lmcache==0.5.8rc1") for x in extra), extra
    assert "pytest==8.4.2" in extra and "pluggy==1.6.0" in extra \
        and "iniconfig==2.1.0" in extra, extra
    assert "setuptools-scm==9.2.0" in extra, extra
    # the OK line must NOT list the noise
    for noise in ("pytest==8.4.2", "lmcache==0.5.8rc1", "setuptools-scm==9.2.0"):
        assert noise not in r.stdout, (
            f"the OK line must not list {noise!r}:\n{r.stdout[-800:]}")


def test_harness_engine_mutation_still_refused(tmp_path):
    """(b) an ENGINE mutation (numpy) must still be refused even though the
    freeze carries non-engine churn."""
    r = _run(tmp_path, skip_tests="0", mutate_on="pytest")
    assert r.returncode != 0, (
        f"an engine mutation must be refused (rc={r.returncode})\n"
        f"stdout:\n{r.stdout[-1500:]}")
    assert "BUILD REFUSED" in r.stderr, r.stderr[-800:]
    assert "engine packages changed" in r.stderr, r.stderr[-800:]