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