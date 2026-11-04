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


def _promotion_loop(script: Path) -> str:
    text = script.read_text()
    start = text.index("for _pair in \"KV_CACHE_DTYPE:KVP_KV_CACHE_DTYPE\"")
    end = text.index("done", start) + len("done")
    return text[start:end]


def test_operator_names_promoted_to_kvp(tmp_path):
    """Review (round 27): an explicitly-set operator name must override the
    KVP_* pin (promoted BEFORE the resolver), never be silently ignored."""
    script = ROOT / "scripts" / "run-vllm-with-persist.sh"
    loop = _promotion_loop(script)
    body = loop + '\nprintf "%s %s %s %s\\n" "$KVP_KV_CACHE_DTYPE" "$KVP_MAX_MODEL_LEN" "$KVP_TP_SIZE" "$KVP_SERVED_MODEL_NAME"'
    cases = [
        # (operator env, pinned KVP_* env, expected KVP_* after promotion)
        ({"KV_CACHE_DTYPE": "fp8", "MAX_MODEL_LEN": "524288", "TP_SIZE": "2",
          "SERVED_MODEL_NAME": "my-deployment"},
         {"KVP_KV_CACHE_DTYPE": "bfloat16", "KVP_MAX_MODEL_LEN": "1048576",
          "KVP_TP_SIZE": "4", "KVP_SERVED_MODEL_NAME": "mimo-pro"},
         "fp8 524288 2 my-deployment"),
        # nothing set -> the pins stay
        ({},
         {"KVP_KV_CACHE_DTYPE": "bfloat16", "KVP_MAX_MODEL_LEN": "1048576",
          "KVP_TP_SIZE": "4", "KVP_SERVED_MODEL_NAME": "mimo-pro"},
         "bfloat16 1048576 4 mimo-pro"),
    ]
    for op_env, pin_env, expected in cases:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **pin_env,
            **op_env,
        }
        out = subprocess.run(
            ["bash", "-c", body], env=env, capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == expected, (
            f"operator env {op_env} + pins {pin_env} must give {expected!r}, "
            f"got {out.stdout.strip()!r} -- operator names must override the "
            f"identity.env pins (they were silently ignored before round 27)"
        )
