#!/usr/bin/env bash
# verify-image-custom.sh -- thin CLI wrapper over kvpersist.check_image_identity.
#
# r2 P0-1 (review): the recipe launchers (recipes/…/run-tp4-hotsplit.sh and
# run-tp4-uva.sh) must refuse to launch ANY image that is not the custom
# LMCache derivative built by build-lmcache.sh + build-image.sh: after the
# local race-guard removal only the custom build carries the PR #4136/#5972
# fixes. A stock vLLM image would silently revert to upstream LMCache and the
# engine dies on the first retrieve/abort race.
#
# Usage:
#   bash scripts/verify-image-custom.sh <IMAGE> <kvp-repo-root>   # 0 = OK to launch
#                                                                      # non-0 = refuse
#
# IMAGE forms accepted:  <repo>:<tag>@sha256:<64hex>   (remote digest pin)
#                        sha256:<64hex>                (bare image ID pin -- local images)
set -Eeuo pipefail
IMG="${1:-}"
ROOT="${2:-}"
[ -n "$IMG" ] || { echo "ERROR(verify-image-custom): usage: $0 <IMAGE> <kvp-repo-root>" >&2; exit 4; }
[ -d "$ROOT/implementation" ] || { echo "ERROR(verify-image-custom): '$ROOT' is not a kv-persistence checkout" >&2; exit 4; }
export PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m kvpersist.check_image_identity "$IMG" "$ROOT/vendor/patches/lmcache"