# SPDX-License-Identifier: Apache-2.0
"""r4 P0-1: CUDA dev-package suffix must follow torch.version.cuda correctly,
`apt-get update` must precede the install, apt failures must stay visible.

Reproduced defects (r3's static tests did not catch any of them):
  * `DEV_SUFFIX="${CUDA_TORCH%.*}"` maps "13.0" -> "13", so apt looked for
    cuda-cudart-dev-13 (does not exist) and the build always died "BUILD
    REFUSED". The nvidia suffix is major-minor: 13-0.
  * no `apt-get update` before the install -- the vLLM image ends its Dockerfile
    with `rm -rf /var/lib/apt/lists/*`, so every install failed "Unable to
    locate package";
  * `>/dev/null 2>&1` hid apt's real error text.

Behavioural tests here run the REAL suffix helper and the REAL apt block
extracted from build-lmcache.sh, with a stubbed apt-get/python3.
"""
from __future__ import annotations
import os
import pathlib
import re
import subprocess
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
BUILD_IMG = ROOT / "scripts" / "build-image.sh"
SUFFIX_SH = ROOT / "scripts" / "_cuda_dev_suffix.sh"


# ---------------------------------------------------------------------------
# Suffix computation (behavioural, acceptance criteria values)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("torch_cuda,expected", [("13.0", "13-0"), ("13.0.2", "13-0"), ("12.8", "12-8")])
def test_dev_package_suffix_follows_torch_cuda(torch_cuda, expected):
    r = subprocess.run(["bash", str(SUFFIX_SH), torch_cuda],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected, (
        f"torch.version.cuda={torch_cuda} must map to dev suffix {expected}, "
        f"got {r.stdout.strip()!r}"
    )


# ---------------------------------------------------------------------------
# The apt block of build-lmcache.sh, re-hosted with a stubbed apt-get
# ---------------------------------------------------------------------------
def _extract_apt_block() -> str:
    src = BUILD_LMC.read_text()
    m = re.search(r'^CUDA_TORCH="\$\(python3.*?\nfor pkg in .*?^done$',
                  src, re.M | re.S)
    assert m, (
        "build-lmcache.sh must contain the in-container block computing "
        "CUDA_TORCH -> DEV_SUFFIX plus the apt install loop "
        "(from `CUDA_TORCH=\"$(python3` to `done`)"
    )
    block = m.group(0)
    # in the container the helper is mounted at /kvp-scripts; point at the repo copy
    block = block.replace("/kvp-scripts/_cuda_dev_suffix.sh", str(SUFFIX_SH))
    return block


def _run_apt_block(tmp_path: pathlib.Path, torch_cuda: str,
                   apt_update_rc: int = 0, apt_install_rc: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    apt_log = tmp_path / "apt.log"
    (bin_dir / "apt-get").write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$@\" >> {apt_log}\n"
        f"if [ \"$1\" = \"update\" ]; then exit {apt_update_rc}; fi\n"
        "echo 'stub apt output line'\n"
        "echo 'E: Unable to locate package stub-reason' >&2\n"
        f"exit {apt_install_rc}\n"
    )
    (bin_dir / "apt-get").chmod(0o755)
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        f"if echo \"$*\" | grep -q 'torch.version.cuda'; then echo '{torch_cuda}'; exit 0; fi\n"
        "exit 0\n"
    )
    (bin_dir / "python3").chmod(0o755)
    script = tmp_path / "block.sh"
    script.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + _extract_apt_block() + "\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    return r, apt_log


def test_install_requests_correct_packages(tmp_path):
    """The loop must request the 13-0-versioned dev packages for torch 13.0
    (the bug requested cuda-cudart-dev-13)."""
    r, apt_log = _run_apt_block(tmp_path, "13.0", apt_install_rc=0)
    assert r.returncode == 0, f"block must succeed:\n{r.stdout}\n{r.stderr}"
    calls = apt_log.read_text()
    for pkg in ("cuda-cudart-dev-13-0", "libcusparse-dev-13-0",
                "libcusolver-dev-13-0", "libcublas-dev-13-0"):
        assert pkg in calls, f"apt must be asked for {pkg}:\n{calls}"
    assert not re.search(r"(?m)^install .*cuda-cudart-dev-13$", calls), (
        f"must NOT request the wrong '13' suffix:\n{calls}"
    )


def test_apt_update_happens_before_install(tmp_path):
    """Without apt-get update the image's purged apt lists make every install
    'Unable to locate package'. Verify ordering: update, then installs."""
    r, apt_log = _run_apt_block(tmp_path, "13.0", apt_install_rc=0)
    assert r.returncode == 0
    lines = [ln for ln in apt_log.read_text().splitlines() if ln]
    assert lines, "apt-get must be called"
    assert lines[0].startswith("update"), (
        f"first apt-get call must be 'update -y', got: {lines[0]}"
    )
    assert any(ln.startswith("install") for ln in lines), "installs must follow"


def test_apt_update_failure_refuses_build(tmp_path):
    r, _ = _run_apt_block(tmp_path, "13.0", apt_update_rc=100)
    assert r.returncode != 0, "apt-get update failure must abort"
    assert "BUILD REFUSED" in (r.stdout + r.stderr)


def test_apt_error_text_is_visible_on_refusal(tmp_path):
    """The old >/dev/null 2>&1 hid apt's reason. The refusal must carry the
    last lines of apt's own output."""
    r, _ = _run_apt_block(tmp_path, "13.0", apt_install_rc=100)
    assert r.returncode != 0, "missing dev package must abort"
    err = r.stdout + r.stderr
    assert "Unable to locate package" in err, (
        f"the real apt error line must remain visible in the refusal:\n{err}"
    )
    assert "BUILD REFUSED" in err


# ---------------------------------------------------------------------------
# Remaining static wiring checks from r3 (still valid)
# ---------------------------------------------------------------------------
def test_in_container_installs_cuda_dev_packages():
    src = BUILD_LMC.read_text()
    for pkg in ("cuda-cudart-dev", "libcusparse-dev", "libcusolver-dev", "libcublas-dev"):
        assert pkg in src, f"{pkg} must be apt-installed in the build container"
    assert "apt-get install" in src
    assert "--no-install-recommends" in src


def test_install_is_fail_loud():
    src = BUILD_LMC.read_text()
    for ln in src.splitlines():
        if "apt-get install" in ln:
            assert "|| true" not in ln, (
                "apt-get install must NOT be fail-soft -- a missing dev "
                "package must abort the build (r3 P0-3 point 2)"
            )


def test_cuda_home_exported():
    src = BUILD_LMC.read_text()
    assert re.search(r"export CUDA_HOME=|CUDA_HOME=/usr/local/cuda", src)


def test_dev_packages_do_not_leak_into_final_image():
    df = BUILD_IMG.read_text()
    assert "apt-get" not in df
    assert "-dev-" not in df