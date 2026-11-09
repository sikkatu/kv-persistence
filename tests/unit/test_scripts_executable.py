"""Round 78 follow-up (user finding): every launcher/helper script must be
EXECUTABLE (git mode 100755). The recipe's scripts/ shipped with 100644 --
`bash scripts/check-hotsplit-tp4.sh` worked but `./scripts/...` and any tool
that exec()s the script directly (test-recovery hooks, kvp, systemd units)
failed with Permission denied. Check the FILESYSTEM bit (what a git checkout
produces from the committed mode)."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def test_all_kv_scripts_are_executable():
    missing = []
    for p in sorted(SCRIPTS.iterdir()):
        if p.suffix in (".sh", ".py") or p.name == "kvp":
            if not os.access(p, os.X_OK):
                missing.append(p.name)
    assert not missing, (
        f"scripts without the executable bit (git mode must be 100755): {missing}")


def test_scripts_committed_with_exec_bit():
    """The COMMITTED mode is what a fresh clone gets -- filesystem chmod alone
    is not enough (a fresh `git clone` restores the committed mode)."""
    import subprocess
    r = subprocess.run(["git", "ls-files", "-s", "scripts/"],
                       capture_output=True, text=True, cwd=str(ROOT))
    bad = []
    for line in r.stdout.splitlines():
        mode, _sha, _stage, path = line.split(None, 3)
        if path.endswith((".sh", ".py")) or path.endswith("/kvp") or path == "scripts/kvp":
            if mode != "100755":
                bad.append(f"{mode} {path}")
    assert not bad, (
        f"committed script modes must be 100755, got: {bad}")