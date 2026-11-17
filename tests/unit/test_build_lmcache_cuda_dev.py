# SPDX-License-Identifier: Apache-2.0
"""r3 P0-3: the in-container build must install CUDA dev headers before
compiling cuda_ops, and must FAIL LOUDLY if any package is unavailable.

The vLLM runtime image ships cuda-nvcc + runtime but NOT the headers needed to
compile the LMCache CUDA extension (cuda-cudart-dev, libcusparse-dev,
libcusolver-dev -- see vLLM's own Dockerfile comments around its source-build
stage). Without installing them the wheel falls back to pure-Python (the
cudaMemcpy error 1 lineage) or dies unhelpfully on a missing cuda_runtime.h.

Wiring contract asserted here (the actual container build is exercised on the
operator stand per r3 P0-3 point 4):
  * `apt-get install` of exactly the pinned dev packages, version-suffixed
    (e.g. cuda-cudart-dev-13-0 derived from torch.version.cuda),
  * escalation: if any package is unavailable the script must abort with a
    message naming the missing package, not proceed into an opaque compiler
    error (test: the install command is fail-loud, not `|| true`),
  * `export CUDA_HOME=/usr/local/cuda` present,
  * dev packages must NOT leak into the final image (the Dockerfile must not
    install them -- only the one-shot build container does).
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
BUILD_IMG = ROOT / "scripts" / "build-image.sh"


def test_in_container_installs_cuda_dev_packages():
    src = BUILD_LMC.read_text()
    for pkg in ("cuda-cudart-dev", "libcusparse-dev", "libcusolver-dev", "libcublas-dev"):
        assert pkg in src, f"{pkg} must be apt-installed in the build container"
    assert "apt-get install" in src, "dev packages must go through apt-get install"
    assert "--no-install-recommends" in src, (
        "keep the dev install minimal (runtime image hygiene)"
    )


def test_dev_package_suffix_follows_torch_cuda():
    """13.0.x must map to the `13-0` package suffix (nvidia's naming)."""
    src = BUILD_LMC.read_text()
    assert "torch.version.cuda" in src or "TORCH_CUDA_SHORT" in src, (
        "package suffix must be derived from the image's torch.version.cuda "
        "(13.0 -> 13-0), not hardcoded"
    )
    assert "13-0" in src or "${DEV_SUFFIX" in src or "DEV_SUFFIX" in src, (
        "the 13-0 suffix (or its computed shell variable) must appear"
    )


def test_install_is_fail_loud():
    """A missing package must abort with its name -- not a silent `|| true`."""
    src = BUILD_LMC.read_text()
    # find the apt-get install line and assert it is not rescued by `|| true`
    for ln in src.splitlines():
        if "apt-get install" in ln:
            assert "|| true" not in ln, (
                "apt-get install must NOT be fail-soft -- a missing dev package "
                "must abort the build (r3 P0-3 point 2)"
            )
    assert re.search(r"apt-get.*\|\| *die|die .*package|missing.*package", src, re.M | re.I), (
        "the script must have an explicit failure path naming the missing package"
    )


def test_cuda_home_exported():
    src = BUILD_LMC.read_text()
    assert re.search(r"export CUDA_HOME=|CUDA_HOME=/usr/local/cuda", src), (
        "CUDA_HOME=/usr/local/cuda must be exported for nvcc/cmake discovery"
    )


def test_dev_packages_do_not_leak_into_final_image():
    """The derivative Dockerfile must only install the wheel -- dev packages
    belong to the throwaway build container, never the running image."""
    df = BUILD_IMG.read_text()
    assert "apt-get" not in df, (
        "build-image.sh's Dockerfile must not apt-install anything (dev packages "
        "stay in the one-shot build container)"
    )
    assert "-dev-" not in df, "no CUDA dev packages in the final image Dockerfile"