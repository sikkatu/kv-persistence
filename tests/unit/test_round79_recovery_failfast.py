"""Round 79 regression (kv-persistence side): recovery fail-fast + diagnostics.

The reviewer's hardware run showed test-recovery.sh waiting out the engine's
300 s register_kv_caches timeout while the cache server had ALREADY failed
('Error handling operation' / CUDA_ERROR_MAP_FAILED), then aborting with a
hint ("check: docker logs") instead of printing the logs. Round 79:
* api_ready() fail-fasts on a fresh 'Error handling operation' in the server
  log (root cause printed, abort) and PRINTS docker logs when the container
  dies -- no manual forensics;
* scenario B runs the shared CUDA IPC pre-flight BEFORE `docker start` (the
  exact step that killed B);
* every server-log read uses grep -a (NUL bytes make plain grep treat the
  file as binary and silently skip it);
* start-all.sh's recipe branch passes the SAME-UID flag to the shared
  pre-flights (the recipe engine runs as $(id -u):$(id -g) since round 79).
"""
from __future__ import annotations

from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
RECOVERY = KV / "scripts" / "test-recovery.sh"
START_ALL = KV / "scripts" / "start-all.sh"


@pytest.fixture(scope="module")
def recovery_text() -> str:
    assert RECOVERY.is_file(), "scripts/test-recovery.sh is missing"
    return RECOVERY.read_text()


@pytest.fixture(scope="module")
def start_all_text() -> str:
    assert START_ALL.is_file(), "scripts/start-all.sh is missing"
    return START_ALL.read_text()


def test_api_ready_fail_fasts_on_server_error(recovery_text):
    """api_ready must abort on a fresh 'Error handling operation' in the
    server log instead of waiting 300 s on a dead handshake."""
    assert "Error handling operation" in recovery_text, (
        "test-recovery.sh must scan the server log for handler errors")
    # the mark must be captured at entry (fresh bytes only)
    assert "_srv_mark" in recovery_text, (
        "api_ready must track the log mark to scan only FRESH bytes")
    idx = recovery_text.index("api_ready()")
    seg = recovery_text[idx:idx + 3000]
    assert "fail-fast" in seg, "api_ready must label the abort as fail-fast"


def test_api_ready_prints_docker_logs_on_death(recovery_text):
    """When the container dies the logs are PRINTED, not hinted at."""
    idx = recovery_text.index("api_ready()")
    seg = recovery_text[idx:idx + 3000]
    assert "docker logs --tail 60" in seg, (
        "api_ready must print docker logs when the engine dies")
    assert "automatic" in seg, (
        "the printed logs must be labelled automatic (no manual re-runs)")


def test_scenario_b_ipc_preflight_before_docker_start(recovery_text):
    """Scenario B must run the CUDA IPC pre-flight BEFORE `docker start` --
    that is exactly where B died (register_kv_cache / cuIpcOpenEventHandle).
    Round 79 review: the path must be $HERE/ipc-test.sh (HERE already points
    at scripts/) -- $HERE/scripts/ipc-test.sh does not exist and the [ -x ]
    guard silently SKIPPED the whole block (the old substring test missed it)."""
    b_start = recovery_text.index('docker start "$CONTAINER"')
    before = recovery_text[max(0, b_start - 2500):b_start]
    assert "$HERE/ipc-test.sh" in before, (
        "the IPC pre-flight must call $HERE/ipc-test.sh before `docker start`")
    assert "$HERE/scripts/ipc-test.sh" not in recovery_text, (
        "scripts/scripts/ipc-test.sh does not exist -- the -x guard would skip")
    assert "pre-flight" in before.lower(), (
        "the B pre-flight must announce itself")


def test_fail_fast_pattern_targets_register_only(recovery_text):
    """Round 79 review: the trigger must match 'Error handling operation
    register_kv_cache' -- the broad 'Error handling operation' also matches
    the benign 'unregister_kv_cache' / ZMQError: Host unreachable noise and
    false-triggers fail-fast after the log mark."""
    trigger = "grep -a -m1 'Error handling operation register_kv_cache'"
    assert trigger in recovery_text, (
        "the fail-fast trigger must be narrowed to register_kv_cache")
    assert "grep -a -m1 'Error handling operation'" not in recovery_text, (
        "the broad pattern would match benign unregister_kv_cache noise")


def test_server_log_reads_use_grep_a(recovery_text):
    """Every read of the server log (NUL bytes) must use grep -a -- plain grep
    treats it as binary and silently skips matches."""
    import re
    for m in re.finditer(r"\| grep (-\w*c?\w*E|\-\w+)\s", recovery_text):
        flags = m.group(1)
        assert "a" in flags, (
            f"server-log grep without -a (binary file silently skipped): "
            f"grep {flags}")
    assert "grep -acE 'L2 store completed" in recovery_text, (
        "the L2 store counter must use grep -a")


def test_start_all_recipe_branch_passes_same_uid(start_all_text):
    """The shared pre-flights must be told the engine's UID mode -- the recipe
    engine runs same-UID since round 79, so _same must follow KVP_SAME_UID,
    not a hardcoded 0 (which tested root->user while the engine runs as the
    user)."""
    assert '"${KVP_SAME_UID:-1}"' in start_all_text, (
        "start-all.sh must pass ${KVP_SAME_UID:-1} to _kvp_pre_flights")
    # the old hardcoded root-mode call must be gone
    assert '"$(id -g)" "0"' not in start_all_text, (
        "the pre-flights must not hardcode _same=0 (root producer)")