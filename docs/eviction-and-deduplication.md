> Multilingual docs: [English](https://github.com/sikkatu/kv-persistence/wiki/Storage-and-Eviction) | [Русский](https://github.com/sikkatu/kv-persistence/wiki/Storage-and-Eviction.ru) | [中文](https://github.com/sikkatu/kv-persistence/wiki/Storage-and-Eviction.zh)

# Eviction and deduplication

## Eviction policy

Two layers cooperate:

1. **Automatic (adapter-native, always on).** LMCache `nixl_store_dynamic` runs a
   background thread over `get_usage()`. Above `trigger_watermark` (default 0.9
   of `max_capacity_gb`) it evicts with the configured policy (`LRU`) until
   usage drops by `eviction_ratio` (default 0.1 of used capacity per cycle).
   This guarantees the hard limit and the soft threshold behavior without any
   external scheduler.

2. **Value-based ranking (policy layer).** `implementation/kvpersist/retention.py`
   orders entries to evict using only measured signals:
   * recency (last access timestamp, exponential decay 24 h half-life),
   * reuse frequency (hit count),
   * benefit of a hit (prefix length in tokens),
   * storage cost (bytes actually used), and shared-references penalty.

   Value score = (3·hits + 1.5·ln(1+tokens)) × (0.3 + 0.7·recency) ÷ bytes (scaled),
   with deterministic tie-break by key. `select_victims()` returns the minimal
   set of least-valuable entries covering the required bytes and skips
   "valuable long prefixes" (tokens ≥ `min_keep_tokens` **and** hits > 0) until
   cheaper candidates are exhausted. The math is offline-testable — see
   `tests/unit/test_budget_and_retention.py` (stale evicted before recent, rare
   before frequent, huge never-reused evicted before small junk, long reused
   prefix survives a full crisis).

### Conflict resolution

LMCache applies its own LRU when the fine-grained per-entry metadata is not
available (it is not exposed by every backend). That limitation is honest:
`retention.py` is applied where (and only where) entry-level statistics exist —
the documented limitation is in docs/final-report.md ("PARTIAL: value-based
retention is implemented and tested offline; in-service application depends on
per-entry stats exposure").

## Properties the policy guarantees

* Frequently reused long prefixes are retained (they are the most expensive to
  recompute and the most likely to be reused).
* A huge never-reused entry cannot displace the useful cache indefinitely: its
  value/bytes ratio is tiny so it ranks first for eviction.
* Ties are deterministic; reports show which keys were retained/evicted and why,
  without any prompt text (`eviction_report` → counts + keys only).
* Deletion preserves shared blocks: only the minimal object set is deleted and
  shared objects stay while any key references them (below).

## Deduplication and shared blocks

LMCache stores KV chunks under content-derived keys. Two conversations with the
same stable prefix produce the same chunk keys ⇒ the second store is a no-op
(hit on the existing object), not a duplicate file. That IS the deduplication
mechanism:

* Same model + tokenizer + dtype + template ⇒ identical prefix tokens ⇒
  identical chunk keys ⇒ single stored object per chunk.
* Different models/revision/template ⇒ different namespace and different keys ⇒
  never shared (safety first; see docs/architecture.md).
* Shared-reference integrity: the backend deletes an object only when its key is
  evicted; two conversations sharing a key share the object. Evicting one
  conversation's tail never removes its shared prefix object while the other
  conversation still references it (LMCache does not refcount per-object — the
  object key mapping itself is the sharing. This is documented as: dedup is
  safe at chunk granularity; access to the same key by any client keeps it
  alive; keys unused long enough become LRU victims).
* Storage saved through dedup is measurable: the same object file is counted
  once. `budget-report.sh status` accounts actual on-disk bytes; the tests
  measure "bytes stored per conversation with shared prefix" in
  `tests/integration/test_dedup.py`.

If a platform reaches a state where LMCache's key semantics cannot guarantee
safe shared-block deletion (only plausible on hybrid/recurrent layouts, NOT on
MiMo's uniform-attention layout), the fallback documented behavior is: eviction
removes keys; an object with multiple referencing keys is deleted only when the
LRU controller decides the key is dead — correctness for concurrent readers is
preserved because deletion removes the file but a query that misses simply
falls through to recompute. This is a *liveness* trade-off, not a correctness
one, and is spelled out in docs/final-report.md.

## Testing

* Offline (runs anywhere): `tests/unit/test_budget_and_retention.py` — full
  retention policy behavior including priority order and protected prefixes.
* Integration (needs `lmcache` installed, CPU ok): `tests/integration/test_dedup.py`
  — two shared-prefix requests exercise the same chunk keys; measured storage
  for second conversation is smaller than naive double-size.
* Hardware eviction (Test 8 in the brief): run `test-recovery.sh` and
  `benchmark.sh` against a deliberately small `KVP_CACHE_BUDGET_GB` (e.g. 2 GB)
  in an isolated namespace directory to see the automatic eviction fire without
  filling the production NVMe.