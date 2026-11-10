"""Round 80b: resolve the engine launcher / shared modules after the legacy
kv launcher (run-vllm-with-persist.sh) was deleted.

The engine launch is owned by the RECIPE launcher now (run-tp4-hotsplit.sh
with KV_PERSIST=1); the shared checks it and the old launcher both called
live in kv-persistence's shared modules (env.sh etc.). Tests read whichever
file OWNS the contract under test.
"""
from __future__ import annotations

from pathlib import Path

KV_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = KV_ROOT / "scripts"
ENV = SCRIPTS / "env.sh"
START_ALL = SCRIPTS / "start-all.sh"
GATE = SCRIPTS / "verify-engine-identity.sh"
IPC_TEST = SCRIPTS / "ipc-test.sh"
TEST_RECOVERY = SCRIPTS / "test-recovery.sh"
START_CACHE = SCRIPTS / "start-cache.sh"

_RECIPE_REL = Path("recipes") / "rtx-pro-6000" / "mimo-v2.6-pro-hotsplit"


def recipe_launcher() -> Path | None:
    """Resolve the recipe's run-tp4-hotsplit.sh across the known layouts."""
    candidates = [
        KV_ROOT.parent / "recipes" / _RECIPE_REL / "scripts" / "run-tp4-hotsplit.sh",
        KV_ROOT / "recipes" / _RECIPE_REL / "scripts" / "run-tp4-hotsplit.sh",
        Path.home() / "sikkatu-recipes" / _RECIPE_REL / "scripts" / "run-tp4-hotsplit.sh",
        Path.home() / "recipes" / _RECIPE_REL / "scripts" / "run-tp4-hotsplit.sh",
        Path("/home/review/recipes") / _RECIPE_REL / "scripts" / "run-tp4-hotsplit.sh",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def recipe_dir() -> Path | None:
    """Resolve the recipe checkout dir (the parent of scripts/)."""
    p = recipe_launcher()
    return p.parent.parent if p else None


def recipe_hotsplit_env() -> Path | None:
    d = recipe_dir()
    return (d / "scripts" / "hotsplit_env.sh") if d else None


def read(path: Path) -> str:
    return path.read_text()


def contract_text() -> str:
    """Round 80b: the engine contract now lives across the shared modules and
    the recipe launcher (the legacy kv launcher owned it all before). A
    textual `assert "X" in text` check is redirected here so the contract is
    proven to EXIST in the live system -- not lost with the deleted script.
    Each owner is tagged so a failure points at where to look."""
    parts = []
    for tag, path in (
        ("env.sh", ENV),
        ("start-all.sh", START_ALL),
        ("verify-engine-identity.sh", GATE),
        ("ipc-test.sh", IPC_TEST),
        ("test-recovery.sh", TEST_RECOVERY),
        ("start-cache.sh", START_CACHE),
    ):
        if path.is_file():
            parts.append(f"<<<{tag}>>>\n" + path.read_text())
    rl = recipe_launcher()
    if rl is not None:
        parts.append(f"<<<run-tp4-hotsplit.sh>>>\n" + rl.read_text())
    hs = recipe_hotsplit_env()
    if hs is not None:
        parts.append(f"<<<hotsplit_env.sh>>>\n" + hs.read_text())
    return "\n".join(parts)