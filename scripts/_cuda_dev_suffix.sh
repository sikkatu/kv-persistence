#!/usr/bin/env bash
# _cuda_dev_suffix.sh -- compute the NVIDIA CUDA dev-package suffix from a
# `torch.version.cuda` value (e.g. "13.0", "13.0.2", "12.8").
#
# The CUDA dev packages (cuda-cudart-dev-13-0, libcusparse-dev-12-8, ...) use a
# `major-minor` suffix with a dash. Only the FIRST TWO components are kept, so
# a patch-level torch CUDA ("13.0.2") still maps to the "13-0" package suffix.
# This is the single source of truth for that mapping -- the build-lmcache.sh
# in-container phase calls THIS script (mounted read-only) and the unit test
# calls it directly, so the logic cannot drift between the two.
#
# Usage:  bash scripts/_cuda_dev_suffix.sh <torch.version.cuda>
# Prints only the suffix (e.g. "13-0"). Empty input -> empty output.

set -Eeuo pipefail

value="${1:-}"
# keep major.minor only, then dot -> dash. cut is a no-op for "13.0" and drops
# the ".2" of "13.0.2".
printf '%s' "$value" | cut -d. -f1,2 | tr . -
