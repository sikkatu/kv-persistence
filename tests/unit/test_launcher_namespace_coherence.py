"""Cross-launcher namespace coherence (review round 20).

Both launchers must derive the SAME `KVP_SERVED_MODEL_NAME` default for the
same deployment -- otherwise the identical cache identity gets two LMCache
model_names (`mimo-pro/<hash>` vs `MiMo-V2.6-Pro-MOPD/<hash>`) and the two
paths never share cached blocks (reviewer measured exactly this).

The check EXECUTES the real default-assignment line taken from the launcher
script (not a hand-copied constant), so if the launcher regresses to
`basename "$MODEL_DIR"` the test fails.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHERS = [
    ROOT / "scripts" / "run-vllm-with-persist.sh",
]


def _default_expr(script: Path) -> str:
    text = script.read_text()
    m = re.search(
        r'export KVP_SERVED_MODEL_NAME="\$\{KVP_SERVED_MODEL_NAME:-(.+?)\}"',
        text,
    )
    assert m, f"{script.name}: no KVP_SERVED_MODEL_NAME default expression found"
    return m.group(1)


def test_served_model_name_default_matches_served_model_name(tmp_path):
    """The default must follow SERVED_MODEL_NAME (what the engine is called
    via --served-model-name), NOT basename(MODEL_DIR). The recipe's KV_PERSIST
    branch passes KVP_SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-mimo-pro}" -- the
    two must agree for identical deployments to share one cache namespace."""
    for script in LAUNCHERS:
        expr = _default_expr(script)
        model_dir = tmp_path / "MiMo-V2.6-Pro-MOPD"
        model_dir.mkdir()
        for served, expect in (("mimo-pro", "mimo-pro"),
                               ("my-deployment", "my-deployment"),
                               (None, "mimo-pro")):
            env = {
                "MODEL_DIR": str(model_dir),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            if served is not None:
                env["SERVED_MODEL_NAME"] = served
            out = subprocess.run(
                ["bash", "-c", f'KVP_SERVED_MODEL_NAME="{expr}"; printf %s "$KVP_SERVED_MODEL_NAME"'],
                env=env, capture_output=True, text=True, timeout=30,
            )
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == expect, (
                f"{script.name}: default prefix must follow SERVED_MODEL_NAME "
                f"({served!r} -> {expect!r}), got {out.stdout.strip()!r} -- a "
                "basename(MODEL_DIR) default desyncs the two launchers' "
                "namespaces for the SAME identity"
            )
            assert out.stdout.strip() != model_dir.name, (
                f"{script.name}: the default must NOT be basename(MODEL_DIR)"
            )