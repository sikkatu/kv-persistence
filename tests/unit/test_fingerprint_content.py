"""Regression test for the local checkpoint fingerprint algorithm (review P0).

The old implementation hashed only shard NAME + SIZE -- a shard swapped for
different bytes (same name, same size) did NOT rotate the fingerprint, risking
cache reuse against incompatible weights. setup-identity.sh now hashes each
shard's CONTENT (SHA-256). This test reproduces the exact P0 scenario.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup-identity.sh"


def _make_model(dirpath: Path, shard_bytes: bytes):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "config.json").write_text('{"chat_template":"Hi"}')
    (dirpath / "tokenizer.json").write_text("tok")
    (dirpath / "model.index.json").write_text(
        json.dumps({"weight_map": {"a": "s1.safetensors"}}))
    (dirpath / "s1.safetensors").write_bytes(shard_bytes)


def _fingerprint(model_dir: Path) -> str:
    cfg = model_dir / "identity.env.test"
    cfg.write_text((ROOT / "configs/mimo-v2.6-pro/identity.env").read_text())
    out = subprocess.run(
        ["bash", str(SCRIPT), str(model_dir)],
        capture_output=True, text=True,
        env={**os.environ, "CONFIG_FILE": str(cfg)},
    )
    for line in out.stdout.splitlines():
        if "checkpoint fingerprint:" in line:
            return line.split()[-1]
    raise AssertionError(f"fingerprint line not found:\n{out.stdout}\n{out.stderr}")


def test_byte_swap_same_name_same_size_rotates_fingerprint(tmp_path):
    """P0 regression: shard content change must rotate the fingerprint."""
    a = tmp_path / "a"
    _make_model(a, b"SHARDCONTENT-AAA")   # 16 bytes
    f1 = _fingerprint(a)

    b = tmp_path / "b"
    _make_model(b, b"SHARDCONTENT-ZZZ")   # 16 bytes, SAME length, different bytes
    f2 = _fingerprint(b)

    assert f1 != f2, (
        "byte-level shard swap with same name+size did NOT rotate the identity "
        f"fingerprint (got {f1} == {f2}) -- cache could be reused against "
        "incompatible weights (review P0)"
    )


def test_identical_content_is_deterministic(tmp_path):
    a = tmp_path / "a"
    _make_model(a, b"SHARDCONTENT-AAA")
    b = tmp_path / "b"
    _make_model(b, b"SHARDCONTENT-AAA")
    assert _fingerprint(a) == _fingerprint(b)


def test_missing_shard_fails_closed(tmp_path):
    """A shard declared in the index but absent on disk is a hard error, not a skip."""
    d = tmp_path / "d"
    _make_model(d, b"SHARDCONTENT-AAA")
    (d / "s1.safetensors").unlink()
    cfg = d / "identity.env.test"
    cfg.write_text((ROOT / "configs/mimo-v2.6-pro/identity.env").read_text())
    out = subprocess.run(
        ["bash", str(SCRIPT), str(d)],
        capture_output=True, text=True,
        env={**os.environ, "CONFIG_FILE": str(cfg)},
    )
    assert out.returncode != 0 or "missing on disk" in out.stdout, (
        "a missing declared shard must not silently produce a fingerprint")