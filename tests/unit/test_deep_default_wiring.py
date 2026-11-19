# SPDX-License-Identifier: Apache-2.0
"""r4 P0-3: deep image verification must run BY DEFAULT, and it must compare
the REAL installed lmcache version against the baked identity.

Reproduced: check_image_identity.py --deep is only invoked by hand --
verify-image-custom.sh (line 23) and _kvp_require_custom_image (env.sh ~719)
call the module WITHOUT --deep, so every real launch stayed label-only despite
r3 P1-3. Also the identity JSON could claim any wheel_version; only
importlib.metadata.version('lmcache') proves what is really installed.

Behavioural tests: stub `docker`, run the REAL entry points
(scripts/verify-image-custom.sh and the env.sh function) WITHOUT extra flags
and assert rc, and run the deep logic with a version cross-check.
"""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
PATCH_DIR = ROOT / "vendor" / "patches" / "lmcache"
VERIFY_SH = ROOT / "scripts" / "verify-image-custom.sh"
PY = sys.executable
HEX64 = "0" * 64
GOOD_SPEC = f"sha256:{HEX64}"
WHEEL_VERSION = "0.5.8rc1.post48+kvp.deadbeefcafe"


def _sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _good_labels() -> dict:
    return {
        "org.kvp.lmc-base": f"vllm/vllm-openai:nightly-8e2beaa3@sha256:{'b' * 64}",
        "org.kvp.lmc-patch-4136": _sha256(PATCH_DIR / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha256(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
    }


def _good_identity(wheel_version: str = WHEEL_VERSION) -> dict:
    return {
        "pr4136_patch_sha256": _sha256(PATCH_DIR / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha256(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
        "wheel_sha256": "a" * 64,
        "wheel_version": wheel_version,
    }


def _docker_stub(bin_dir: pathlib.Path, labels: dict, identity: dict | str | None,
                 installed_version: str | None = WHEEL_VERSION,
                 cuda_ops_ok: bool = True):
    """Stub answering every verification sub-command. identity=None simulates
    a failing `cat /etc/kvp-lmcache-identity.json` (the r4 repro case)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    ident_text = json.dumps(identity) if isinstance(identity, dict) else (identity or "null")
    cuda_rc = "0" if cuda_ops_ok else "1"
    if installed_version:
        ver_body = f"echo '{installed_version}'"
    else:
        ver_body = "echo 'No module named lmcache' >&2; exit 1"
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "cmd=\"$1\"; shift || true\n"
        "if [ \"$cmd\" = \"image\" ] || [ \"$cmd\" = \"inspect\" ]; then\n"
        f"  echo '{json.dumps(labels)}'; exit 0\n"
        "fi\n"
        "if [ \"$cmd\" = \"run\" ]; then\n"
        "  case \"$*\" in\n"
        "    *'/etc/kvp-lmcache-identity.json'*)\n"
        f"      if [ '{ident_text}' = 'null' ]; then echo 'cat: no such file' >&2; exit 1; fi\n"
        f"      echo '{ident_text}'; exit 0 ;;\n"
        "    *'importlib.util'*)\n"
        f"      exit {cuda_rc} ;;\n"
        "    *'importlib.metadata'*)\n"
        f"      {ver_body} ;;\n"
        "  esac\n"
        "  echo ''\n"
        "  exit 0\n"
        "fi\n"
        "echo ''\nexit 0\n"
    )
    (bin_dir / "docker").chmod(0o755)
    return bin_dir


def _run_verify_sh(tmp_path: pathlib.Path, labels, identity, installed_version=WHEEL_VERSION,
                   cuda_ops_ok=True, shallow_env=None):
    bin_dir = tmp_path / "bin"
    _docker_stub(bin_dir, labels, identity, installed_version, cuda_ops_ok)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    if shallow_env is not None:
        env["KVP_VERIFY_SHALLOW"] = shallow_env
    else:
        env.pop("KVP_VERIFY_SHALLOW", None)
    return subprocess.run(
        ["bash", str(VERIFY_SH), GOOD_SPEC, str(ROOT)],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Entry points must pass --deep WITHOUT any extra flags (the r4 repro)
# ---------------------------------------------------------------------------
def test_verify_sh_deep_by_default_fails_when_identity_unreadable(tmp_path):
    """LABELs all correct, but `cat /etc/kvp-lmcache-identity.json` fails:
    verify-image-custom.sh must refuse rc!=0 WITHOUT any flags -- proving the
    deep check ran (the label-only path is forgeable)."""
    r = _run_verify_sh(tmp_path, labels=_good_labels(), identity=None)
    assert r.returncode != 0, (
        f"verify-image-custom.sh must refuse when the identity json cannot be "
        f"read (deep by default), got rc={r.returncode}:\n{r.stdout}\n{r.stderr}"
    )
    assert "identity" in (r.stdout + r.stderr)


def test_start_cache_require_custom_image_deep(tmp_path):
    """_kvp_require_custom_image (used by start-cache.sh docker mode) must also
    deep-verify by default. Simulated via the same module call the function
    uses, with the docker stub -- a stub whose `cat` fails must yield rc!=0."""
    bin_dir = tmp_path / "bin"
    _docker_stub(bin_dir, _good_labels(), identity=None)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               PYTHONPATH=str(IMPL))
    env.pop("KVP_VERIFY_SHALLOW", None)
    # exactly what _kvp_require_custom_image execs after r4 P0-3
    r = subprocess.run(
        [PY, "-m", "kvpersist.check_image_identity", GOOD_SPEC,
         str(PATCH_DIR), "--deep"],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path),
    )
    assert r.returncode != 0, (
        "the deep call used by _kvp_require_custom_image must refuse when the "
        f"identity json cannot be read, got rc={r.returncode}"
    )
    # and prove the wiring source actually passes --deep by default now
    envsh = (ROOT / "scripts" / "env.sh").read_text()
    assert "KVP_VERIFY_SHALLOW" in envsh, (
        "env.sh must gate --deep behind KVP_VERIFY_SHALLOW (deep by default)"
    )
    assert 'check_image_identity "$img" "$root/vendor/patches/lmcache" "${_verify_args[@]}"' in envsh, (
        "env.sh _kvp_require_custom_image must pass --deep via _verify_args"
    )
    vsh = VERIFY_SH.read_text()
    assert 'KVP_VERIFY_ARGS+=(--deep)' in vsh, (
        "verify-image-custom.sh must add --deep by default"
    )


def test_shallow_optout_keeps_label_only(tmp_path):
    """KVP_VERIFY_SHALLOW=1 must disable the deep probes (test-stand escape
    hatch) so a label-correct image with unreadable identity passes there."""
    r = _run_verify_sh(tmp_path, labels=_good_labels(), identity=None,
                       shallow_env="1")
    assert r.returncode == 0, (
        f"KVP_VERIFY_SHALLOW=1 must allow a label-only pass, got rc={r.returncode}:\n"
        f"{r.stdout}\n{r.stderr}"
    )


# ---------------------------------------------------------------------------
# Real installed version must match the baked identity (r4 P0-3 point 3)
# ---------------------------------------------------------------------------
def test_deep_matches_installed_version(tmp_path):
    r = _run_verify_sh(tmp_path, labels=_good_labels(), identity=_good_identity(),
                       installed_version=WHEEL_VERSION)
    assert r.returncode == 0, (
        f"version match must pass: rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    )


def test_deep_refuses_version_mismatch(tmp_path):
    """Identity claims one wheel_version, the REAL metadata says another:
    refuse."""
    r = _run_verify_sh(tmp_path, labels=_good_labels(),
                       identity=_good_identity(),
                       installed_version="0.5.8rc1.post48+kvp.000000000000")
    assert r.returncode != 0, (
        f"installed version != identity wheel_version must refuse, rc={r.returncode}"
    )
    assert "wheel_version" in (r.stdout + r.stderr) or "version" in (r.stdout + r.stderr)


def test_deep_refuses_version_without_kvp_local_part(tmp_path):
    """A wheel_version WITHOUT the '+kvp.' local part is not a custom build:
    refuse even before comparing."""
    r = _run_verify_sh(tmp_path, labels=_good_labels(),
                       identity=_good_identity(wheel_version="0.5.8rc1.post48"),
                       installed_version="0.5.8rc1.post48")
    assert r.returncode != 0, "version without '+kvp.' must be refused"
    assert "+kvp" in (r.stdout + r.stderr)


def test_deep_refuses_when_metadata_missing(tmp_path):
    """importlib.metadata.version('lmcache') cannot run -> the wheel is not
    installed -> refuse."""
    r = _run_verify_sh(tmp_path, labels=_good_labels(), identity=_good_identity(),
                       installed_version=None)
    assert r.returncode != 0, "missing installed metadata must be refused"