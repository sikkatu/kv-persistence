"""Regression test for the BLOCKING identity-gate bug (review round 21).

The gate `scripts/verify-engine-identity.sh` used to exit 4 (MISMATCH) on the
STANDARD config: the four OPTIONAL fields (KVP_ROPE_CONFIG_DIGEST,
KVP_LORA_ADAPTERS, KVP_MULTIMODAL_COMPONENTS, KVP_SPECULATIVE_CONFIG) ship
empty, and the gate compared the empty `expect` against a display placeholder
"(unset -- not applicable...)" -- they never matched, so every pre-launch
check failed and `run-vllm-with-persist.sh` refused to start.

This test EXECUTES the real gate script (no test did before -- which is why
the bug survived review). It must:
  * PASS (exit 0) on a standard config where only the REQUIRED fields are
    filled and the OPTIONAL ones are left empty;
  * still FAIL (exit 4) when a REQUIRED field is empty.
"""
from __future__ import annotations

import os
import sys
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "verify-engine-identity.sh"
CONFIG_DIR = ROOT / "configs" / "mimo-v2.6-pro"


def _write_config(tmp_path: Path, **overrides: str) -> Path:
    """Copy the shipped identity.env and fill REQUIRED fields with plausible
    values; leave OPTIONAL fields empty (exactly the shipped state)."""
    base = (CONFIG_DIR / "identity.env").read_text()
    required = {
        "KVP_MODEL_REVISION": "0" * 40,
        "KVP_TOKENIZER_REVISION": "0" * 40,
        "KVP_CHAT_TEMPLATE_DIGEST": "0" * 64,
    }
    required.update(overrides)
    text = base
    for name, value in required.items():
        text = re.sub(r"^(" + name + r")=.*$", r"\1=" + value, text, flags=re.M)
    local = CONFIG_DIR / "identity.env.local"
    local.write_text(text)
    return local


@pytest.fixture()
def _cleanup_local():
    local = CONFIG_DIR / "identity.env.local"
    yield local
    if local.exists():
        local.unlink()


def test_gate_passes_on_standard_config_with_empty_optional_fields(tmp_path, _cleanup_local):
    """BLOCKING-BUG regression: empty OPTIONAL fields must NOT fail the gate."""
    _write_config(tmp_path)
    result = subprocess.run(
        ["bash", str(GATE), "--check-config-only"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "verify-engine-identity.sh must PASS (exit 0) on the standard config "
        f"with empty optional fields, got exit {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_gate_still_fails_when_required_field_is_empty(tmp_path, _cleanup_local):
    """The fix must not weaken the gate: an empty REQUIRED field must fail."""
    _write_config(tmp_path, KVP_MODEL_REVISION="")
    result = subprocess.run(
        ["bash", str(GATE), "--check-config-only"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 4, (
        "an empty REQUIRED field (KVP_MODEL_REVISION) must still yield exit 4, "
        f"got {result.returncode}\nstdout:\n{result.stdout}"
    )
    assert "KVP_MODEL_REVISION" in result.stdout


def test_gate_env_parsing_matches_other_loaders(tmp_path, _cleanup_local):
    """Review: the gate's env parser must accept `export`, single quotes and
    trailing comments -- exactly like scripts/env.sh and the Python loader."""
    local = CONFIG_DIR / "identity.env.local"
    base = (CONFIG_DIR / "identity.env").read_text()
    text = base
    text = re.sub(r"^KVP_MODEL_REVISION=.*$", "export KVP_MODEL_REVISION='" + "0" * 40 + "' # pinned rev",
                  text, flags=re.M)
    text = re.sub(r"^KVP_TOKENIZER_REVISION=.*$", 'KVP_TOKENIZER_REVISION="' + "0" * 40 + '"',
                  text, flags=re.M)
    text = re.sub(r"^KVP_CHAT_TEMPLATE_DIGEST=.*$", "KVP_CHAT_TEMPLATE_DIGEST=" + "0" * 64,
                  text, flags=re.M)
    local.write_text(text)
    result = subprocess.run(
        ["bash", str(GATE), "--check-config-only"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "the gate must parse `export`, single quotes and trailing comments the "
        f"same way as env.sh / the Python loader, got exit {result.returncode}\n"
        f"stdout:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Post-launch (container-args) tests with a stub docker (review round 22).
# The blocking connector bug lived in this branch and no test reached it.
# ---------------------------------------------------------------------------

DOCKER_STUB = """#!/usr/bin/env bash
case "$1" in
  inspect)
    if [ -n "${STUB_CMD_ARGS:-}" ]; then
      python3 -c 'import json,os; print(json.dumps(os.environ["STUB_CMD_ARGS"].split("|")))'
    else
      cat <<'JSON'
["--model","/models/x","--max-model-len","1048576","--tensor-parallel-size","4","--kv-cache-dtype","bfloat16","--block-size","16","--kv-transfer-config","{\"kv_connector\":\"LMCacheMPConnector\",\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\"kv_role\":\"kv_both\"}"]
JSON
    fi
    exit 0 ;;
  logs) echo ""; exit 0 ;;
esac
exit 0
"""

KVTC = ('{"kv_connector":"LMCacheMPConnector",'
        '"kv_connector_module_path":"lmcache.integration.vllm.lmcache_mp_connector",'
        '"kv_role":"kv_both"}')
BASE_ARGS = ("--model|/models/x|--max-model-len|1048576|--tensor-parallel-size|4|"
             "--kv-cache-dtype|bfloat16|--block-size|16|--kv-transfer-config|" + KVTC)


@pytest.fixture()
def _stub_env(tmp_path):
    """A stub docker on PATH + a model dir whose config.json matches the pins."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text(
        '{"torch_dtype":"bfloat16","num_key_value_heads":8,"head_dim":128}'
    )
    import os
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["MODEL_DIR"] = str(model)
    # Clear any KVP_* overrides the caller's shell may carry -- the pins in
    # identity.env(.local) are the comparison baseline for these tests.
    for k in list(env):
        if k.startswith("KVP_"):
            del env[k]
    return env


def _run_gate(env, cmd_args, extra=None):
    import os
    e = env.copy()
    e["STUB_CMD_ARGS"] = cmd_args
    e.update(extra or {})
    return subprocess.run(
        ["bash", str(GATE), "test-container"],
        capture_output=True, text=True, timeout=60, env=e,
    )


def test_postlaunch_args_match_pins_passes(tmp_path, _cleanup_local, _stub_env):
    """BLOCKING-BUG regression (round 22): when the container args EQUAL the
    identity pins (including the full --kv-transfer-config JSON), the
    post-launch gate must PASS. It used to exit 4 forever because the
    connector check compared the whole JSON against the short pin string."""
    _write_config(tmp_path)
    result = _run_gate(_stub_env, BASE_ARGS)
    assert result.returncode == 0, (
        "post-launch gate must pass when args == pins, got exit "
        f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_postlaunch_connector_in_json_is_accepted(tmp_path, _cleanup_local, _stub_env):
    """The connector pin must match when PRESENT anywhere in the
    --kv-transfer-config JSON (review round 22: the found value must be the
    pin, not the JSON)."""
    _write_config(tmp_path)
    result = _run_gate(_stub_env, BASE_ARGS)
    assert result.returncode == 0
    assert "connector: identity=LMCacheMPConnector" in result.stdout


def test_postlaunch_wrong_connector_is_mismatch(tmp_path, _cleanup_local, _stub_env):
    """A --kv-transfer-config that does NOT reference the pinned connector
    must be a MISMATCH (exit 4)."""
    _write_config(tmp_path)
    bad = BASE_ARGS.replace("LMCacheMPConnector", "WrongConnector")
    result = _run_gate(_stub_env, bad)
    assert result.returncode == 4, (
        f"a wrong connector must be exit 4, got {result.returncode}\n{result.stdout}"
    )
    assert "connector" in result.stdout


def test_postlaunch_max_model_len_override_needs_env(tmp_path, _cleanup_local, _stub_env):
    """A changed engine arg is a MISMATCH against the file pins (safe miss),
    and passes once the operator exports the matching KVP_* override -- the
    documented env-wins precedence (review round 22)."""
    _write_config(tmp_path)
    changed = BASE_ARGS.replace("1048576", "524288")
    result = _run_gate(_stub_env, changed)
    assert result.returncode == 4, (
        f"a changed --max-model-len must mismatch the file pins, got {result.returncode}"
    )
    result2 = _run_gate(_stub_env, changed, {"KVP_MAX_MODEL_LEN": "524288"})
    assert result2.returncode == 0, (
        f"KVP_MAX_MODEL_LEN=524288 must make the gate pass (env wins), got "
        f"{result2.returncode}\n{result2.stdout}"
    )


def test_postlaunch_dtype_override_needs_env(tmp_path, _cleanup_local, _stub_env):
    """Same contract for kv-cache dtype: fp8 vs the bfloat16 pin is a MISMATCH
    unless the operator exports KVP_KV_CACHE_DTYPE=fp8 (review round 22)."""
    _write_config(tmp_path)
    changed = BASE_ARGS.replace("bfloat16", "fp8")
    result = _run_gate(_stub_env, changed)
    assert result.returncode == 4
    result2 = _run_gate(_stub_env, changed, {"KVP_KV_CACHE_DTYPE": "fp8"})
    assert result2.returncode == 0, (
        f"KVP_KV_CACHE_DTYPE=fp8 must make the gate pass (env wins), got "
        f"{result2.returncode}\n{result2.stdout}"
    )


def test_postlaunch_auto_dtype_resolves_to_model_dtype(tmp_path, _cleanup_local, _stub_env):
    """--kv-cache-dtype auto is resolved to the model's dtype (config.json),
    so an auto launch of a bfloat16 model passes against the bfloat16 pin."""
    _write_config(tmp_path)
    changed = BASE_ARGS.replace("bfloat16", "auto")
    result = _run_gate(_stub_env, changed)
    assert result.returncode == 0, (
        f"'auto' must resolve to the model dtype and pass, got {result.returncode}"
        f"\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Round 23: shared effective-identity resolver (recipe operator names).
# ---------------------------------------------------------------------------

def test_operator_env_names_resolve_to_effective_values(tmp_path, _cleanup_local, _stub_env):
    """Review (round 23): the recipe launchers configure the engine with
    KV_CACHE_DTYPE / MAX_MODEL_LEN / TP_SIZE / SERVED_MODEL_NAME and derive the
    cache namespace from them. The manual gate run must compare the container
    args against the SAME effective values -- a non-default run (fp8, 524288,
    TP=2) must PASS, not be blocked by MISMATCH against the file pins."""
    _write_config(tmp_path)
    args = BASE_ARGS.replace("1048576", "524288").replace("bfloat16", "fp8").replace(
        "--tensor-parallel-size|4", "--tensor-parallel-size|2")
    result = _run_gate(_stub_env, args, {
        "KV_CACHE_DTYPE": "fp8",
        "MAX_MODEL_LEN": "524288",
        "TP_SIZE": "2",
        "SERVED_MODEL_NAME": "my-dep",
    })
    assert result.returncode == 0, (
        "a non-default recipe-style run (fp8 + 524288 + tp2) must verify clean "
        f"against its own effective values, got exit {result.returncode}\n"
        f"stdout:\n{result.stdout}"
    )


def test_operator_names_consistent_with_namespace_resolver(tmp_path):
    """The shared resolver (kvpersist.cli effective-identity) must be the SINGLE
    source of truth: its output is what the recipe launchers hash the namespace
    from AND what the gate compares against. Directly verify the CLI output
    reflects the operator names."""
    cli = ROOT / "implementation" / "kvpersist" / "cli.py"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "KV_CACHE_DTYPE": "fp8",
        "MAX_MODEL_LEN": "524288",
        "TP_SIZE": "2",
        "SERVED_MODEL_NAME": "my-dep",
        "PYTHONPATH": str(ROOT / "implementation"),
    }
    out = subprocess.run(
        [sys.executable, "-m", "kvpersist.cli", "effective-identity",
         "--model-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert out.returncode == 0, out.stderr
    lines = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
    assert lines.get("KVP_KV_CACHE_DTYPE") == "fp8", lines
    assert lines.get("KVP_MAX_MODEL_LEN") == "524288", lines
    assert lines.get("KVP_TP_SIZE") == "2", lines
    assert lines.get("KVP_SERVED_MODEL_NAME") == "my-dep", lines


def test_operator_names_auto_resolves_via_shared_code(tmp_path):
    """KV_CACHE_DTYPE=auto must resolve to the model dtype through the shared
    code (config.json), matching what the recipe launchers hash."""
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"torch_dtype": "bfloat16"}))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "KV_CACHE_DTYPE": "auto",
        "PYTHONPATH": str(ROOT / "implementation"),
    }
    out = subprocess.run(
        [sys.executable, "-m", "kvpersist.cli", "effective-identity",
         "--model-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert out.returncode == 0, out.stderr
    lines = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
    assert lines.get("KVP_KV_CACHE_DTYPE") == "bfloat16", lines


# ---------------------------------------------------------------------------
# Round 26: L2 codec check + KVP_LMCACHE_* propagation.
# ---------------------------------------------------------------------------

def test_l2_serde_mismatch_is_caught(tmp_path, _cleanup_local, _stub_env):
    """Review (round 26): the L2 codec is part of the cache identity. A gate
    shell with USE_LOSSLESS_SERDE=1 (zstd chunks on the server) against an
    UNPINNED identity must MISMATCH -- chunks written with one codec would
    otherwise be read back with another."""
    _write_config(tmp_path)
    result = _run_gate(_stub_env, BASE_ARGS, {"USE_LOSSLESS_SERDE": "1"})
    assert result.returncode == 4, (
        f"USE_LOSSLESS_SERDE=1 vs an unpinned l2_serde must mismatch, got "
        f"{result.returncode}\n{result.stdout}"
    )
    assert "l2_serde" in result.stdout


def test_l2_serde_pin_makes_it_pass(tmp_path, _cleanup_local, _stub_env):
    """A matching KVP_L2_SERDE pin (zstd) with USE_LOSSLESS_SERDE=1 must pass."""
    _write_config(tmp_path)
    result = _run_gate(_stub_env, BASE_ARGS,
                       {"USE_LOSSLESS_SERDE": "1", "KVP_L2_SERDE": "zstd"})
    assert result.returncode == 0, (
        f"KVP_L2_SERDE=zstd + USE_LOSSLESS_SERDE=1 must pass, got "
        f"{result.returncode}\n{result.stdout}"
    )


def test_kvp_lmcache_duplicates_propagate_to_lmcache_names(tmp_path):
    """Review (round 26): KVP_LMCACHE_* / KVP_L1_SIZE_GB in cache.env(.local)
    must reach the LMCACHE_* names the server reads -- they used to be
    silently ignored (server started with the shipped defaults)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "KVP_LMCACHE_PORT": "6000",
        "KVP_L1_SIZE_GB": "32",
        "PYTHONPATH": str(ROOT / "implementation"),
    }
    # run the env.sh loader in a clean shell and print the effective values
    out = subprocess.run(
        ["bash", "-c",
         '. scripts/env.sh && _kvp_load_env "$PWD" >/dev/null 2>&1; '
         'printf "LMCACHE_PORT=%s\nLMCACHE_L1_SIZE_GB=%s\n" '
         '"${LMCACHE_PORT:-}" "${LMCACHE_L1_SIZE_GB:-}"'],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(ROOT),
    )
    assert out.returncode == 0, out.stderr
    lines = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
    assert lines.get("LMCACHE_PORT") == "6000", (
        f"KVP_LMCACHE_PORT=6000 must propagate to LMCACHE_PORT: {lines}"
    )
    assert lines.get("LMCACHE_L1_SIZE_GB") == "32", (
        f"KVP_L1_SIZE_GB=32 must propagate to LMCACHE_L1_SIZE_GB: {lines}"
    )
