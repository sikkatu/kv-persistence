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
    # The l2_serde check reads the SERVER's rendered adapter (written by
    # start-cache.sh at launch). Provide one by default: raw chunks ("none").
    cache_root = tmp_path / "cache_root"
    cache_root.mkdir(exist_ok=True)
    (cache_root / "l2-adapter.rendered.json").write_text("{}")
    env["KVP_CACHE_ROOT"] = str(cache_root)
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
    """Review (round 27): the l2_serde check verifies the SERVER's codec (from
    l2-adapter.rendered.json) against the namespace the cache is filed under.
    A server started with zstd (rendered adapter) while the namespace was
    hashed as "none" (no USE_LOSSLESS_SERDE in the gate/launcher shell) MUST
    MISMATCH -- chunks written with one codec would otherwise be read back
    with another."""
    _write_config(tmp_path)
    cache_root = Path(_stub_env["KVP_CACHE_ROOT"])
    (cache_root / "l2-adapter.rendered.json").write_text(
        '{"serde": {"type": "kvp-zstd", "level": "3", "align_bytes": "4096"}}'
    )
    result = _run_gate(_stub_env, BASE_ARGS)
    assert result.returncode == 4, (
        f"a zstd server against a none namespace must mismatch, got "
        f"{result.returncode}\n{result.stdout}"
    )
    assert "l2_serde" in result.stdout


def test_l2_serde_namespace_follows_server(tmp_path, _cleanup_local, _stub_env):
    """When the namespace WAS hashed with the same codec the server uses
    (USE_LOSSLESS_SERDE=1 -> zstd, server rendered zstd), the gate passes --
    this is the README's `USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh`
    scenario (review round 27: it used to fail with exit 4)."""
    _write_config(tmp_path)
    cache_root = Path(_stub_env["KVP_CACHE_ROOT"])
    (cache_root / "l2-adapter.rendered.json").write_text(
        '{"serde": {"type": "kvp-zstd", "level": "3", "align_bytes": "4096"}}'
    )
    result = _run_gate(_stub_env, BASE_ARGS, {"USE_LOSSLESS_SERDE": "1"})
    assert result.returncode == 0, (
        f"the README scenario (shell switch == server codec) must pass, got "
        f"{result.returncode}\n{result.stdout}"
    )


def test_l2_serde_missing_adapter_is_inconclusive(tmp_path, _cleanup_local, _stub_env):
    """Without the rendered adapter the gate cannot PROVE the server's codec --
    it must report INCONCLUSIVE (exit 5), never silently pass."""
    _write_config(tmp_path)
    cache_root = Path(_stub_env["KVP_CACHE_ROOT"])
    (cache_root / "l2-adapter.rendered.json").unlink()
    result = _run_gate(_stub_env, BASE_ARGS)
    assert result.returncode == 5, (
        f"missing l2-adapter.rendered.json must be INCONCLUSIVE, got "
        f"{result.returncode}\n{result.stdout}"
    )
    assert "l2-adapter.rendered.json" in result.stdout


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


# ---------------------------------------------------------------------------
# Round 28: layered alias resolution + KVP_L2_SERDE pin in the namespace.
# ---------------------------------------------------------------------------

def test_layered_alias_resolution(tmp_path):
    """Review (round 28, item 1): an LMCACHE_* edit in lmcache-server.env.local
    must win over the shipped KVP_* aliases (cache.env). The aliases are
    commented out now; a layer-local edit is never overridden."""
    import subprocess as _sp
    root = tmp_path / "repo"
    (root / "configs" / "generic").mkdir(parents=True)
    (root / "configs" / "mimo-v2.6-pro").mkdir(parents=True)
    (root / "configs" / "generic" / "cache.env").write_text(
        "# KVP_L1_SIZE_GB=64\n# KVP_LMCACHE_PORT=5555\n# KVP_CHUNK_SIZE=256\n")
    (root / "configs" / "generic" / "lmcache-server.env").write_text(
        "LMCACHE_L1_SIZE_GB=64\nLMCACHE_PORT=5555\nLMCACHE_CHUNK_SIZE=256\n")
    (root / "configs" / "generic" / "lmcache-server.env.local").write_text(
        "LMCACHE_L1_SIZE_GB=128\nLMCACHE_PORT=6000\nLMCACHE_CHUNK_SIZE=512\n")
    (root / "configs" / "mimo-v2.6-pro" / "identity.env").write_text(
        "KVP_MODEL_ID=x\n")
    out = _sp.run(
        [sys.executable, "-c",
         "import sys; from kvpersist.config import load_config; "
         "c = load_config(sys.argv[1], env_wins=False); "
         "print(c.get('LMCACHE_L1_SIZE_GB'), c.get('LMCACHE_PORT'), "
         "c.get('LMCACHE_CHUNK_SIZE'))", str(root)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(ROOT / "implementation")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "128 6000 512", (
        f"lmcache-server.env.local edits must win over the KVP_* aliases, "
        f"got {out.stdout.strip()!r}"
    )


def test_kvp_l2_serde_pin_reaches_namespace(tmp_path):
    """Review (round 28, item 2): a KVP_L2_SERDE pin in identity.env must
    change the NAMESPACE (zstd pin != raw). Previously the pin was ignored
    for the namespace while the gate accepted it -- zstd chunks landed under
    the raw namespace."""
    import subprocess as _sp
    root = tmp_path / "repo"
    (root / "configs" / "generic").mkdir(parents=True)
    (root / "configs" / "mimo-v2.6-pro").mkdir(parents=True)
    (root / "configs" / "generic" / "cache.env").write_text("")
    (root / "configs" / "generic" / "lmcache-server.env").write_text("LMCACHE_L2_SERDE=none\n")
    (root / "configs" / "mimo-v2.6-pro" / "identity.env").write_text(
        "KVP_MODEL_ID=x\nKVP_MODEL_REVISION=" + "0" * 40 +
        "\nKVP_TOKENIZER_NAME=x\nKVP_TOKENIZER_REVISION=" + "0" * 40 +
        "\nKVP_KV_CACHE_DTYPE=bfloat16\nKVP_BLOCK_SIZE=16\nKVP_NUM_KV_HEADS=8"
        "\nKVP_HEAD_DIM=128\nKVP_TP_SIZE=4\nKVP_CONNECTOR=c\n"
        "KVP_CONNECTOR_VERSION=v\nKVP_MAX_MODEL_LEN=1\n"
        "KVP_CHAT_TEMPLATE_DIGEST=" + "0" * 64 + "\n")
    code = (
        "import os, sys; "
        "from kvpersist.identity import identity_from_env_files; "
        "[os.environ.pop(k, None) for k in "
        "('LMCACHE_L2_SERDE','KVP_L2_SERDE','USE_LOSSLESS_SERDE')]; "
        "g = os.path.join; base = g(sys.argv[1], 'configs/generic'); "
        "mc = g(sys.argv[1], 'configs/mimo-v2.6-pro'); "
        "paths = [g(base, 'cache.env'), g(base, 'lmcache-server.env'), "
        "g(mc, 'identity.env')]; "
        "print(identity_from_env_files(*paths).namespace())"
    )
    out_raw = _sp.run([sys.executable, "-c", code, str(root)],
                      capture_output=True, text=True, timeout=30,
                      env={**os.environ, "PYTHONPATH": str(ROOT / "implementation")})
    # now set the pin
    (root / "configs" / "mimo-v2.6-pro" / "identity.env").write_text(
        (root / "configs" / "mimo-v2.6-pro" / "identity.env").read_text() +
        "KVP_L2_SERDE=zstd\n")
    out_pin = _sp.run([sys.executable, "-c", code, str(root)],
                      capture_output=True, text=True, timeout=30,
                      env={**os.environ, "PYTHONPATH": str(ROOT / "implementation")})
    assert out_raw.returncode == 0, out_raw.stderr
    assert out_pin.returncode == 0, out_pin.stderr
    assert out_raw.stdout.strip() != out_pin.stdout.strip(), (
        "a KVP_L2_SERDE=zstd pin must change the namespace "
        f"(raw {out_raw.stdout.strip()} vs pinned {out_pin.stdout.strip()})"
    )


# ---------------------------------------------------------------------------
# Round 29: KVP_CACHE_ROOT fallback parses like env.sh (shared parser).
# ---------------------------------------------------------------------------

def test_cache_root_fallback_expands_home(tmp_path, monkeypatch):
    """Review (round 29): `export KVP_CACHE_ROOT=$HOME/kv-cache   # comment`
    in cache.env must resolve to the REAL home-expanded path -- the gate then
    finds <that>/l2-adapter.rendered.json instead of a literal "$HOME/..."."""
    import subprocess as _sp
    fake_home = tmp_path / "home"
    (fake_home / "kv-cache").mkdir(parents=True)
    (fake_home / "kv-cache" / "l2-adapter.rendered.json").write_text("{}")
    repo = tmp_path / "repo"
    (repo / "configs" / "generic").mkdir(parents=True)
    (repo / "configs" / "mimo-v2.6-pro").mkdir(parents=True)
    (repo / "configs" / "generic" / "cache.env").write_text(
        "# comment line\nexport KVP_CACHE_ROOT=$HOME/kv-cache   # trailing comment\n")
    (repo / "configs" / "generic" / "lmcache-server.env").write_text("")
    (repo / "configs" / "mimo-v2.6-pro" / "identity.env").write_text("KVP_MODEL_ID=x\n")

    code = (
        "import os, sys; "
        "from kvpersist.config import read_env_file; "
        "os.environ['HOME'] = sys.argv[2]; "
        "v = read_env_file(os.path.join(sys.argv[1], 'configs/generic/cache.env')); "
        "print(v.get('KVP_CACHE_ROOT', ''))"
    )
    out = _sp.run(
        [sys.executable, "-c", code, str(repo), str(fake_home)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "HOME": str(fake_home),
             "PYTHONPATH": str(ROOT / "implementation")},
    )
    assert out.returncode == 0, out.stderr
    got = out.stdout.strip()
    expected = str(fake_home / "kv-cache")
    assert got == expected, (
        f"KVP_CACHE_ROOT=$HOME/kv-cache must expand to {expected!r}, got {got!r} "
        f"-- the gate must use the shared parser (export / trailing comment / "
        f"$HOME) like env.sh, not a hand-rolled regex"
    )


# ---------------------------------------------------------------------------
# Round 30: end-to-end gate test + tab-comment parser parity.
# ---------------------------------------------------------------------------

def test_tab_comment_parsers_agree(tmp_path):
    """Review (round 30): bash env.sh and both Python parsers must cut
    trailing comments at space AND tab before '#' identically. The old bash
    ${_val%% #*} only cut at a space; `KEY=value<TAB># comment` kept the
    comment while Python dropped it."""
    env_file = tmp_path / "test.env"
    env_file.write_text("KVP_CACHE_ROOT=/mnt/nvme/kv-cache\t# comment\n")
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[2]); "
         "from kvpersist.config import read_env_file; "
         "from kvpersist.identity import _read_env_config; "
         "c = read_env_file(sys.argv[1]).get('KVP_CACHE_ROOT',''); "
         "i = _read_env_config(sys.argv[1]).get('KVP_CACHE_ROOT',''); "
         "print('config=' + repr(c) + ' identity=' + repr(i))",
         str(env_file), str(ROOT / "implementation")],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "'/mnt/nvme/kv-cache'" in out.stdout, (
        f"tab-comment must be cut identically in both Python parsers, got {out.stdout}"
    )
    assert "comment" not in out.stdout, (
        f"trailing comment after TAB must be stripped, got {out.stdout}"
    )
    # bash side
    out_bash = subprocess.run(
        ["bash", "-c",
         f". {ROOT}/scripts/env.sh; _kvp_parse_line 'KVP_CACHE_ROOT=/mnt/nvme/kv-cache\t# comment'; echo $_val"],
        capture_output=True, text=True, timeout=30,
    )
    assert out_bash.stdout.strip() == "/mnt/nvme/kv-cache", (
        f"bash parser must cut tab-comment identically, got {out_bash.stdout.strip()!r}"
    )


def test_gate_end_to_end_with_home_expansion(tmp_path, _stub_env, _cleanup_local):
    """Review (round 30): the REAL gate must run end-to-end with a
    KVP_CACHE_ROOT=$HOME/kv-cache fallback -- proving .local-over-base
    selection, adapter discovery, real codec read and exit 0."""
    import subprocess as _sp
    fake_home = tmp_path / "home"
    cache_dir = fake_home / "kv-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "l2-adapter.rendered.json").write_text(
        '{"serde": {"type": "kvp-zstd", "level": "3", "align_bytes": "4096"}}'
    )
    _write_config(tmp_path)
    local = CONFIG_DIR / "identity.env.local"
    local.write_text(local.read_text().rstrip() + chr(10) + "KVP_L2_SERDE=zstd" + chr(10))

    cache_env = ROOT / "configs" / "generic" / "cache.env"
    original_cache_env = cache_env.read_text()
    cache_env.write_text(
        original_cache_env + chr(10) + "export KVP_CACHE_ROOT=$HOME/kv-cache   # r30" + chr(10)
    )
    try:
        env = dict(_stub_env)
        env.pop("KVP_CACHE_ROOT", None)  # force the $HOME fallback
        env["HOME"] = str(fake_home)
        env["KVP_L2_SERDE"] = "zstd"
        env["STUB_CMD_ARGS"] = BASE_ARGS
        result = _sp.run(
            ["bash", str(GATE), "test-container"],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        cache_env.write_text(original_cache_env)

    assert result.returncode == 0, (
        f"gate must pass end-to-end with $HOME fallback, got exit "
        f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "identity verified" in result.stdout, (
        f"gate must report identity verified, got:\n{result.stdout[-800:]}"
    )
    assert "l2_serde" in result.stdout and "zstd" in result.stdout, (
        f"gate must read zstd from the rendered adapter, got:\n{result.stdout[-800:]}"
    )
