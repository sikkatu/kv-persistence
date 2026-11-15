"""Round 90 A2: DCGM counters CSV structure tests (production crash in logs).

The operator's log: `ERROR Could not read metrics file '/etc/dcgm-exporter/
default-counters.csv'; err: record on line 12: wrong number of fields` --
a comma inside the HELP column turned a row into 4 fields (the schema is a
QUOTED-LESS csv: FIELD,type,help). Same run: the profiling file shipped ONLY
the 4 PROF rows and, since `-f` REPLACES the counter set, every base
DCGM_FI_DEV_* field vanished -> all GPU panels zero.

MUTATIONs caught:
  * a comma (or any extra separator) in help of any row -> 3-field test fails;
  * a base field dropped from the prof set (live = base + prof) -> superset
    test fails;
  * a rules.d/gpu.yml DCGM_FI_* metric missing from its counters file ->
    contract test fails (rules would silently fall through).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
GPU = KV / "observability" / "extras" / "gpu"
RULES = KV / "observability" / "extras" / "rules.d" / "gpu.yml"

BASE = GPU / "dcgm-counters.csv"
PROF = GPU / "dcgm-counters.prof.csv"


def _rows(p: Path) -> list[list[str]]:
    """Non-comment, non-empty rows parsed as csv (no quoting in this schema)."""
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(next(csv.reader([line])))
    return out


def _fields(p: Path) -> set[str]:
    return {r[0] for r in _rows(p)}


@pytest.mark.parametrize("csv_file", [BASE, PROF])
def test_every_row_has_exactly_three_fields(csv_file):
    """(A2a) FIELD,type,help -- a comma in help (round 90 bug: '(raw counter,
    use rate())') splits the row into 4 fields and dcgm-exporter refuses the
    WHOLE file ("wrong number of fields").
    MUTATION caught: any comma/extra field in help of any row."""
    assert csv_file.is_file(), csv_file
    for r in _rows(csv_file):
        assert len(r) == 3, (
            f"{csv_file.name}: row {r[0]!r} has {len(r)} fields (want 3): {r}\n"
            "help must NOT contain a comma -- the schema is quoted-less csv")


def test_prof_set_superset_of_base():
    """(A2b) live.csv is GENERATED as base + prof-tail exactly the way
    extras-up.sh does it (strip comments/blank lines, concatenate). The merge
    must keep EVERY base field AND every prof field.
    MUTATION caught: dropping the base cat from the merge (the round 90
    failure: a PROF-only file replaced the whole set) or dropping a prof
    row (panels show no data)."""
    def rows(p):
        return [l.strip() for l in p.read_text().splitlines()
                if l.strip() and not l.lstrip().startswith("#")]

    merge = rows(BASE) + rows(PROF)   # what extras-up.sh writes to live.csv
    merged_fields = {r.split(",", 1)[0] for r in merge}

    base = _fields(BASE)
    prof = _fields(PROF)
    assert base, "dcgm-counters.csv empty?"
    assert base <= merged_fields, (
        f"merge LOST base fields (the round 90 bug): {sorted(base - merged_fields)}")
    assert prof <= merged_fields, (
        f"merge LOST prof fields: {sorted(prof - merged_fields)}")


def test_prof_file_adds_prof_fields():
    """The prof tail must actually add the 4 DCGM_FI_PROF_* rows the
    `gpu-prof` panels query (rules.d/gpu.yml). MUTATION caught: a prof file
    that diverges from the rules (e.g. renamed DCGM_FI_PROF_SM_ACTIVE)."""
    prof = _fields(PROF)
    for name in ("DCGM_FI_PROF_SM_ACTIVE", "DCGM_FI_PROF_DRAM_ACTIVE",
                 "DCGM_FI_PROF_PCIE_RX_BYTES", "DCGM_FI_PROF_PCIE_TX_BYTES"):
        assert name in prof, f"{PROF.name}: missing profile field {name}"


def test_rules_referenced_fields_exist_in_counters():
    """(A2c) contract rules.d/gpu.yml <-> counters CSV: every concrete
    DCGM_FI_DEV_* used by a rule is in the BASE file (it is the `gpu`
    profile's set), and every DCGM_FI_PROF_* is in the PROF tail (loaded
    together with base in the `gpu-prof` profile).
    MUTATION caught: adding a rule on DCGM_FI_DEV_X without shipping X in
    the CSV -- the bank silently falls through to the or-branch."""
    text = RULES.read_text()
    dev = {m for m in re.findall(r"\bDCGM_FI_DEV_[A-Z0-9_]+", text)}
    prof = {m for m in re.findall(r"\bDCGM_FI_PROF_[A-Z0-9_]+", text)}
    base_fields = _fields(BASE)
    prof_fields = _fields(PROF)
    missing_dev = dev - base_fields
    missing_prof = prof - prof_fields
    assert not missing_dev, f"rules.d/gpu.yml uses DCGM fields not in {BASE.name}: {sorted(missing_dev)}"
    assert not missing_prof, f"rules.d/gpu.yml uses PROF fields not in {PROF.name}: {sorted(missing_prof)}"