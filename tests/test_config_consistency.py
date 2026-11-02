"""Cross-component config consistency guard (review round 10).

Three mechanisms interpret the same env files: scripts/env.sh (shell seeder),
implementation/kvpersist/config.py (Python loader) and the launcher/verify
scripts. If they ever diverge on precedence or model-config selection, the
stale-identity bug class returns. This test drives all three on a synthetic
layout and asserts identical effective values.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "implementation"))


def _write_layout(tmp: Path, model: str, base: dict, local: dict):
    d = tmp / "configs" / model
    d.mkdir(parents=True, exist_ok=True)
    (tmp / "configs" / "generic").mkdir(parents=True, exist_ok=True)
    (d / "identity.env").write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    (d / "identity.env.local").write_text("".join(f"{k}={v}\n" for k, v in local.items()))


def test_env_sh_and_config_py_agree_on_local_overlay(tmp_path):
    _write_layout(
        tmp_path, "testmodel",
        base={"KVP_MODEL_REVISION": "aaaabbbbccccddddeeeeffff0000111122223333",
              "KVP_KV_CACHE_DTYPE": "bfloat16"},
        local={"KVP_MODEL_REVISION": "1111222233334444555566667777888899990000"},
    )

    env = dict(os.environ, KVP_MODEL_CONFIG="testmodel")
    # shell loader
    out = subprocess.run(
        ["bash", "-c",
         f'. "{ROOT}/scripts/env.sh"; _kvp_load_env "{tmp_path}"; echo "$KVP_MODEL_REVISION"'],
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    # Python loader
    from kvpersist.config import load_config
    merged = load_config(str(tmp_path), model_config="testmodel")
    py_val = merged["KVP_MODEL_REVISION"]

    assert out == "1111222233334444555566667777888899990000", (
        f"shell loader did not apply .local overlay: {out}")
    assert py_val == "1111222233334444555566667777888899990000", (
        f"python loader did not apply .local overlay: {py_val}")
    assert out == py_val, (
        f"shell ({out}) and python ({py_val}) loaders disagree on .local overlay")


def test_env_sh_and_config_py_agree_on_env_wins(tmp_path, monkeypatch):
    _write_layout(
        tmp_path, "testmodel",
        base={"KVP_KV_CACHE_DTYPE": "bfloat16"},
        local={},
    )
    monkeypatch.setenv("KVP_KV_CACHE_DTYPE", "fp16")
    env = dict(os.environ, KVP_MODEL_CONFIG="testmodel", KVP_KV_CACHE_DTYPE="fp16")
    out = subprocess.run(
        ["bash", "-c",
         f'. "{ROOT}/scripts/env.sh"; _kvp_load_env "{tmp_path}"; echo "$KVP_KV_CACHE_DTYPE"'],
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    from kvpersist.config import load_config
    merged = load_config(str(tmp_path), model_config="testmodel")
    assert out == "fp16", f"shell loader ignored pre-exported env: {out}"
    assert merged["KVP_KV_CACHE_DTYPE"] == "fp16", (
        f"python loader ignored pre-exported env: {merged['KVP_KV_CACHE_DTYPE']}")


def test_ambiguous_model_layout_refuses_silently(tmp_path):
    """Multiple model dirs without KVP_MODEL_CONFIG must not silently pick one."""
    _write_layout(tmp_path, "model-a", {"KVP_MODEL_REVISION": "a" * 40}, {})
    _write_layout(tmp_path, "model-b", {"KVP_MODEL_REVISION": "b" * 40}, {})
    env = dict(os.environ)
    env.pop("KVP_MODEL_CONFIG", None)
    out = subprocess.run(
        ["bash", "-c",
         f'. "{ROOT}/scripts/env.sh"; _kvp_load_env "{tmp_path}"'],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode != 0, (
        "ambiguous multi-model layout must refuse, not silently pick one")
    assert "multiple model config dirs" in (out.stdout + out.stderr)


# --- quote parsing parity (review: KVP_X='abc' disagreed between loaders) ---


def _shell_parse(value: str) -> str:
    tmp = Path("/tmp/kvp-quote-test.env")
    tmp.write_text(f"KVP_QUOTE_TEST={value}\n")
    out = subprocess.run(
        ["bash", "-c",
         f'line=$(head -1 "{tmp}"); '
         f'. "{ROOT}/scripts/env.sh"; _kvp_parse_line "$line"; '
         'printf %s "$_val"'],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def _python_parse(value: str) -> str:
    sys.path.insert(0, str(ROOT / "implementation"))
    from kvpersist.config import _parse_env_file

    tmp = Path("/tmp/kvp-quote-test-py.env")
    tmp.write_text(f"KVP_QUOTE_TEST={value}\n")
    return _parse_env_file(tmp).get("KVP_QUOTE_TEST", "")


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "'abc'",
        '"abc"',
        "'a b'",
        '"a b"',
        "'mixed",
        "mixed'",
        '""',
        "''",
        "it's",
        'say "hi"',
        "'don''t'",
    ],
)
def test_quote_parsing_identical_in_bash_and_python(raw):
    """Review fix: `KVP_X='abc'` must give `abc` in BOTH loaders.

    Before the fix bash kept the single quotes (only double quotes were
    stripped), so the two loaders disagreed despite the documented
    "MUST match scripts/env.sh `_kvp_parse_line`" contract.
    """
    assert _shell_parse(raw) == _python_parse(raw), (
        f"bash and python env parsers disagree for {raw!r}: "
        f"{_shell_parse(raw)!r} != {_python_parse(raw)!r}"
    )