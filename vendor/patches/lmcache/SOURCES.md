# LMCache downstream patch sources

This directory holds the **temporary downstream patches** applied to the pinned
LMCache source before the custom package is built. Both patches correspond to
upstream fix PRs that are not yet merged. When they land upstream, delete them
by following the removal procedure in `docs/lmcache-build.md` (§ Future upstream
migration) — do not keep them as permanent technical debt.

Each patch is derived from the **latest upstream PR head reviewed for this
spec** and is applied to the pinned base commit, not to the PR's older base.
**Porting rule (r15):** a patch taken from a PR head MUST pass the static
import gate (`implementation/kvpersist/check_patched_imports.py`, run by
`build-lmcache.sh` right after the patches) on the pinned base before the
build. **"Applies cleanly" does NOT mean "semantically current"** -- upstream
moves/renames (like `IPCCacheServerKey` moving to
`lmcache.v1.common.custom_types`) survive `git apply` fuzz and only fail at
import time inside the 20-minute build. After every pin change, re-run the
gate and port the patch to the new base (fix the stale names in the patch
tree and re-export it with `git diff` -- never hand-edit hunks); never
weaken/skip the gate to make a stale patch pass.

## Pinned base

| Item | Value |
| --- | --- |
| Upstream project | https://github.com/LMCache/LMCache |
| Base source commit | `03ee5b82418b6bd44e424cdd46932d9346f43eb2` |
| Base release identity | `0.5.7.dev428` (nightly of 2026-11-16) |
| Branch | `dev` |

The `nightly` tag is mutable — production builds always check out the exact
commit SHA above, never the floating tag.

## PR #6551 — already merged, included in base (no local patch)

| Item | Value |
| --- | --- |
| Title | [Bugfix] Preserve chunk prefixes when touching prefetched keys |
| URL | https://github.com/LMCache/LMCache/pull/6551 |
| Merge commit | `61faa902a81462177d40d74f8c64bb9df02163c4` |

Verified: `git merge-base --is-ancestor 61faa902... 03ee5b82...` → true, so the
fix is already in the pinned base. **Do not add a local duplicate.** This fix
corrects LRU recency ordering for prefetched keys only; it does **not** make
whole-prefix eviction atomic.

## PR #4136 — retrieve/abort race (temporary downstream patch)

| Item | Value |
| --- | --- |
| Title | [Bugfix] Handle request abort during MP retrieve |
| URL | https://github.com/LMCache/LMCache/pull/4136 |
| Upstream PR head (source revision) | `b10588bd46288192e01ab06da3c3fa923ad0401a` |
| Patch file | `4136-mp-retrieve-abort.patch` |
| Patch SHA-256 | `1da52d7506ae2411984d8f6ee9402881adcc4710157e96a5e0c66003c18a3d0d` |

Implements the explicit `LOADING` retrieve lifecycle
(`PREFETCHING → WAITING_FOR_LOAD → LOADING → READY`), scheduler-confirmed abort
propagation to workers, suppression of invalid store completions and abort-ID
deduplication. Because the PR head was based on an older `dev` revision, the
change was **rebased manually** onto the pinned base; the conflict resolution
merges the newer base features (delay-free / boundary-state blocks, lazy-offload
manager) with the upstream fix. Includes upstream regression tests.

## PR #5972 — incomplete KV-cache objects (temporary downstream patch)

| Item | Value |
| --- | --- |
| Title | Prevent storing incomplete KV-cache objects |
| URL | https://github.com/LMCache/LMCache/pull/5972 |
| Upstream PR head (source revision) | `1ccd6f698794cc45d189c14223d339623abe6ec2` |
| Patch file | `5972-incomplete-kv-objects.patch` |
| Patch SHA-256 | `b80d20c13c8e9e38cbfdfebc5e6005f471adbddd7e58a701e9b815a677472e33` |

Moves the block-availability check after window downsampling so an object whose
retained geometry still contains a `null_block_id` (markers `0` or `-1`) is
rejected instead of being persisted as complete. Preserves independently
complete objects in other groups, merged/separate object groups, compressed and
uncompressed layouts. Includes upstream regression tests. Rebased manually onto
the pinned base (the PR head is far behind `dev`); docs-only upstream changes
were not carried over — project docs describe the behaviour instead.

## Patch file scope

* `4136-mp-retrieve-abort.patch` touches only `lmcache/integration/vllm/*` and
  `tests/v1/*` (vLLM MP integration layer).
* `5972-incomplete-kv-objects.patch` touches only `lmcache/v1/multiprocess/*`
  and `tests/v1/multiprocess/*` (MP cache-engine layer).

The two patches share **no files**, so each is independently removable.
