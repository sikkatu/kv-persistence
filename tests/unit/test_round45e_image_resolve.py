"""Round 45e regression: ipc-test.sh resolves IMAGE itself (copy-paste commands).

The operator rightly rejected commands with placeholders (IMAGE=<your
image>). ipc-test.sh must run as simply `bash scripts/ipc-test.sh`, finding
the pinned digest from the recipe's tp4.env.example via
print-recipe-image.sh, the same way the launcher resolves the recipe.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"
PRINT_IMAGE = ROOT / "scripts" / "print-recipe-image.sh"


def test_image_is_optional_and_resolved_from_recipe(tmp_path):
    """Without IMAGE the script must resolve it from the recipe."""
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    digest = "a" * 64
    (recipe / "tp4.env.example").write_text(
        f'IMAGE="vllm/vllm-openai:nightly-test@sha256:{digest}"\n'
    )
    r = subprocess.run(
        ["bash", str(IPC_TEST)],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "RECIPE_DIR": str(recipe), "IMAGE": ""},
    )
    out = r.stdout + r.stderr
    assert "image resolved from" in out, out
    assert f"sha256:{digest}" in out, out
    # past the resolution it fails on HOST_PY in the sandbox -- that is fine
    assert "HOST_PY" in out or "PASS" in out, out


def test_no_placeholders_in_commands():
    """The script must not instruct the operator to fill placeholders."""
    t = IPC_TEST.read_text()
    assert "IMAGE=..." not in t, "no IMAGE=... placeholders in commands"
    # usage block must show the simple command
    assert "bash scripts/ipc-test.sh" in t
    # the launcher hint must be copy-pasteable too
    lt = (ROOT / "scripts" / "run-vllm-with-persist.sh").read_text()
    assert "IMAGE=$IMAGE bash scripts/ipc-test.sh" not in lt, (
        "the launcher hint must not require the operator to type the image"
    )
    assert "bash scripts/ipc-test.sh" in lt


def test_print_recipe_image_exists():
    """The helper used for resolution must exist and pin by digest."""
    t = PRINT_IMAGE.read_text()
    assert "@sha256:" in t, "print-recipe-image.sh must require a digest pin"