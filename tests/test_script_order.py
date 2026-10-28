"""Guard: shell functions must be declared before first use (review round 10).

Bash resolves function calls at CALL TIME, not parse time: if a function is
used on line N and declared after line N, `set -e` + command-not-found gives
exit 127 BEFORE any verdict runs. This test statically checks every bash
script for the pattern: `name(){ ... }` must appear before the first call
`name ` or `$(name `.
"""

import re
from pathlib import Path

SCRIPTS = sorted(Path(__file__).resolve().parents[2].glob("scripts/*.sh"))
FUNC_DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", re.M)
FUNC_CALL = re.compile(r"(?<![A-Za-z0-9_./])(?:\$\()?([A-Za-z_][A-Za-z0-9_]*)\s")


def test_functions_declared_before_use():
    for script in SCRIPTS:
        if script.name == "env.sh":
            continue  # sourced library, calls are external by design
        text = script.read_text()
        defs = {}
        for m in FUNC_DEF.finditer(text):
            defs.setdefault(m.group(1), text[: m.start()].count("\n") + 1)
        calls = {}
        for m in FUNC_CALL.finditer(text):
            name = m.group(1)
            if name in defs:
                line = text[: m.start()].count("\n") + 1
                calls.setdefault(name, []).append(line)
        for name, def_line in defs.items():
            for call_line in calls.get(name, []):
                assert call_line > def_line, (
                    f"{script.name}: function '{name}' declared at line {def_line} "
                    f"but called at line {call_line} -- will fail with code 127 under set -e"
                )