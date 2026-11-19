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
| Base source commit | `07f1c6bb642f4772606a36eab730ae009ddce08f` |
| Base release identity | `0.5.8rc1.post48` (= `git rev-list --count v0.5.8rc1..<pin>` = 48) |
| Branch | `dev` |

The `nightly` tag is mutable — production builds always check out the exact
commit SHA above, never the floating tag.

## PR #6551 — already merged, included in base (no local patch)

| Item | Value |
| --- | --- |
| Title | [Bugfix] Preserve chunk prefixes when touching prefetched keys |
| URL | https://github.com/LMCache/LMCache/pull/6551 |
| Merge commit | `61faa902a81462177d40d74f8c64bb9df02163c4` |

Verified: `git merge-base --is-ancestor 61faa902... 07f1c6bb6...` → true, so the
fix is already in the pinned base. **Do not add a local duplicate.** This fix
corrects LRU recency ordering for prefetched keys only; it does **not** make
whole-prefix eviction atomic.
## PR #6608 — KVCacheGroup in the MP connector (already merged, included in base)
| Item | Value |
| --- | --- |
| URL | https://github.com/LMCache/LMCache/pull/6608 |
| Merge commit | `bcafb031401d404a5dabae4853f5e23bf9e5685c` |
Verified: `git merge-base --is-ancestor bcafb0314... 07f1c6bb6...` → true. The
r15 P0' upgrade keeps the same ancestor gate as #6551 in `build-lmcache.sh`.

## PR #4136 — retrieve/abort race (temporary downstream patch)

| Item | Value |
| --- | --- |
| Title | [Bugfix] Handle request abort during MP retrieve |
| URL | https://github.com/LMCache/LMCache/pull/4136 |
| Upstream PR head (source revision) | `b10588bd46288192e01ab06da3c3fa923ad0401a` |
| Patch file | `4136-mp-retrieve-abort.patch` |
| Patch SHA-256 | `1f79f3fb8d98c10fe84e03f6abcd4d657fe6a5f5109497405880e670565de637` |
Implements the explicit `LOADING` retrieve lifecycle
(`PREFETCHING → WAITING_FOR_LOAD → LOADING → READY`), scheduler-confirmed abort
propagation to workers, suppression of invalid store completions and abort-ID
deduplication. Because the PR head was based on an older `dev` revision, the
change was **rebased manually** onto the pinned base; the conflict resolution
merges the newer base features (delay-free / boundary-state blocks, lazy-offload
manager) with the upstream fix. Includes upstream regression tests.
**Test fixtures adapted to the pinned base (r16):** the upstream tests were
written against the August PR head and built the connector with only part of
the current `__init__` attributes; the fixture helpers now set
`_delay_free_req_ids` / `_can_store` / `_pinned_boundary_state_blocks` /
`_lazy_offload_manager` and the request stubs carry `num_computed_tokens`;
the obsolete `_pending_store` assertions were rewritten against the
lazy-offload manager API. **Production part of the patch unchanged** -- only
`tests/v1/test_lmcache_mp_connector.py` fixtures.

## PR #5972 — incomplete KV-cache objects (temporary downstream patch)
| Item | Value |
| --- | --- |
| Title | Prevent storing incomplete KV-cache objects (+ unify retained-block validation across MP store paths) |
| URL | https://github.com/LMCache/LMCache/pull/5972 |
| PR base (source revision) | `07f1c6bb642f4772606a36eab730ae009ddce08f` (= the pinned base) |
| PR head (pinned by SHA) | `bd79d4c6acf6096061b5092517698239440cb93b` (2026-11-19 14:43 +08) |
| Patch file | `5972-incomplete-kv-objects.patch` |
| Patch SHA-256 | `0aa55d930edacc8919529ca3423ed3f7cfdbca8f714b5eb170399aafaddb0316` |

**HEAD-MOVES RULE (r15 P0'):** the PR head may move at any time — the patch
is pinned by EXACT SHA (`git diff 07f1c6bb6..bd79d4c6a`, the full 4-commit PR
rebased on `dev`: `8d96611e5` `fccbeeee1` `0a373858f` `bd79d4c6a`). Never
re-export from a floating head without re-verifying the SHAs above. The last
commit ("Unify retained-block validation across MP store paths") is
substantive new code, not just a rebase: 17 files, +409/−117 vs the old
8-file patch.

Verified on the pinned base: 4136 applies cleanly, then this 5972 applies
cleanly (20 files touched together); the r15 P1 static import gate reports
0 problems over the patched tree + the five CPU-gate files. The new diff does
NOT apply to the PREVIOUS pin (planning.py and test_rocm_host_event_wait.py
did not exist there), which is why the pin moved forward to `07f1c6bb6`.

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
