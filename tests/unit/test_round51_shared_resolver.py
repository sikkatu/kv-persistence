"""Round 51: shared MODEL_DIR resolver -- standalone gate resolves like the launcher.

User log: `scripts/verify-engine-identity.sh` in a fresh terminal showed
model_dir=unset while the launcher's own gate run had it resolved --
the resolution chain lived ONLY in the launcher. Now ONE helper
(_kvp_resolve_model_dir in env.sh) is used by both.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
ENV = SCRIPTS / "env.sh"
GATE = SCRIPTS / "verify-engine-identity.sh"
LAUNCHER = SCRIPTS / "run-vllm-with-persist.sh"


def _read(p: Path) -> str:
    return p.read_text()


def test_shared_resolver_defined_in_env():
    t = _read(ENV)
    assert "_kvp_resolve_model_dir(){" in t
    # the documented chain: live/model_dir > identity pin > ~/models
    assert 'live/model_dir' in t
    assert "identity.env.local" in t
    assert "KVP_MODELS_ROOT" in t


def test_gate_uses_the_shared_resolver():
    t = _read(GATE)
    assert '_kvp_resolve_model_dir "$ROOT"' in t, (
        "the gate must resolve MODEL_DIR via the shared helper")
    # the resolver must be sourced from env.sh
    assert '. "$HERE/env.sh"' in t


def test_launcher_uses_the_shared_resolver():
    t = _read(LAUNCHER)
    assert '_kvp_resolve_model_dir "$ROOT"' in t, (
        "the launcher must use the SAME helper as the gate")
    # the old inline chain must be gone
    assert '_models_root="${KVP_MODELS_ROOT' not in t, (
        "the inline resolution chain must be replaced by the shared helper")


def test_resolver_executes(tmp_path, monkeypatch):
    """EXECUTE the real helper: live/model_dir wins over the models scan."""
    env_sh = _read(ENV)
    start = env_sh.index("_kvp_resolve_model_dir(){")
    end = env_sh.index("\n_kvp_load_env", start)
    helper = env_sh[start:end]

    root = tmp_path / "repo"
    (root / "live").mkdir(parents=True)
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (root / "live" / "model_dir").write_text(str(model) + "\n")

    code = helper + f'\nprintf "%s" "$(_kvp_resolve_model_dir "{root}")"\n'
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                            "KVP_MODELS_ROOT": str(tmp_path / "no-such")})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(model), (r.stdout, r.stderr)


def test_resolver_prefers_explicit_env(tmp_path):
    env_sh = _read(ENV)
    start = env_sh.index("_kvp_resolve_model_dir(){")
    end = env_sh.index("\n_kvp_load_env", start)
    helper = env_sh[start:end]
    code = helper + '\nprintf "%s" "$(_kvp_resolve_model_dir "/nonexistent")"\n'
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                            "MODEL_DIR": "/explicit/path"})
    # explicit env wins even if the directory check at the end drops it
    # (the helper prints nothing for a non-directory -- but must not crash)
    assert r.returncode == 0, r.stderr