> 🌐 Language versions: **English** | [Русский](storage-management.ru.md) | [中文](storage-management.zh.md)

# Storage management

## Budget model

| Guard | Value (default) | Enforced by |
|---|---|---|
| Hard cap | `KVP_CACHE_BUDGET_GB=1500` GB (1.5 TB) | `max_capacity_gb` in the L2 adapter: stores above limit are REJECTED |
| Physical ceiling | whatever the pinned NVMe device holds (checked by preflight) | `KVP_CACHE_DEVICE` + free-space check |
| Soft threshold | 0.9 of budget | `trigger_watermark` — eviction starts here |
| Reclaim target | ~0.81 of budget after one cycle (0.9 trigger x 0.9 kept of used) | `eviction_ratio` 0.1 of used capacity per cycle |
| Per-namespace quota | `quotas.json` in cache root (optional) | `kvp-admin quota` policy layer |

The 1.5 TB default is deliberately generous: more prefill history stays on NVMe,
so reopening a conversation saves recomputation time and energy on the first
load. Raise it further only if the pinned device has the free space (preflight
verifies this against `KVP_CACHE_DEVICE`).

## How long can a conversation be?

Two different limits must not be confused:

* **Per-conversation token limit (~1M)** — bounded by the model's context window
  (`max_position_embeddings` in the model's `config.json`), NOT by this cache.
  A single conversation cannot exceed what the model can hold in one context.
* **How much history the cache retains on disk** — bounded ONLY by
  `KVP_CACHE_BUDGET_GB`. There is no token limit here: as long as a topic's
  prefill chain fits the budget, it is kept on NVMe and reused. NOTE: a model
  update that changes `max_model_len` (or any other identity field) moves the
  deployment to a NEW namespace — earlier cache data is NOT auto-reused after
  such a change, because the KV geometry the model uses would differ. That is
  isolation by design (see docs/architecture.md), not a data limitation.

The budget applies ONLY to persistent KV-cache data. Model weights, OS files and
other application data are outside the cache root and are never accounted or
touched.

## Capacity accounting

* Stored data: one file per KV chunk object under `KVP_CACHE_ROOT` (atomic
  temp-file rename on write, so file set is always consistent).
* Index/metadata: the dynamic backend keeps no separate on-disk index — the
  deterministic file namespace IS the index (rebuilt lazily at startup).
  `bash scripts/budget-report.sh status` reports `used_bytes` (data+metadata
  on disk), `free_bytes`, `usage_fraction` and per-namespace byte counts.
* Concurrency: admission is serialized inside the adapter and
  `BudgetLedger.admit()` refuses once the hard cap would be exceeded
  (`test_concurrent_admits_cannot_exceed_budget`).
* **Restart accounting (NEEDS TESTING)**: after `lmcache server` restarts,
  upstream docs describe an in-memory index lazily rebuilt from on-disk file
  sizes as they are touched. Until the pinned LMCache build is confirmed to feed
  those restored sizes into `get_usage()` (which drives `max_capacity_gb`
  enforcement and LRU eviction), do NOT assume pre-restart files remain inside
  the hard cap. Required hardware test: fill cache -> restart cache service ->
  keep writing -> verify `used_bytes` keeps growing and stays bounded.

## Eviction

Automatic eviction is mandatory and always on:

* Background thread watches `get_usage()` per adapter; above `trigger_watermark`
  the policy evicts until usage drops by `eviction_ratio`.
* Policy: LRU over stored chunk keys (adapter-native). For finer value-based
  ordering when per-entry statistics are available (hits, last access, prefix
  length, bytes), `implementation/kvpersist/retention.py` ranks entries:
  stale/rarely-reused/huge-useless first, frequently reused long prefixes last.
* Shared blocks: LMCache stores each chunk as its own content-derived object;
  different keys never alias to the same object file across accounts. There is
  NO caller-side refcount -- concurrent readers that lose an LRU victim simply
  miss and recompute (a liveness trade-off, never a silent wrong read). See
  docs/eviction-and-deduplication.md and the known-limits in docs/final-report.md.

### Manual cleanup (explicit only)

```bash
bash scripts/budget-report.sh purge --namespace kvp-<ns> --yes
bash scripts/budget-report.sh purge --all --yes
```

Never automatic; requires `--yes`; deletion is confined to namespace directories
inside `KVP_CACHE_ROOT` (path safety checks; refusal exit code 2 otherwise).

### Storage-pressure warnings

`budget-report.sh status` reports `soft_pressure: true` when usage ≥ 90 % of
budget. The launcher also warns at startup when free filesystem space is below
the configured budget.

## Namespaces and cleanup policy

Each compatibility identity maps to one `kvp-<hash>` directory. Model revision
changes create NEW namespaces; old ones are NEVER auto-deleted (the brief
requires explicit documented policy). To reclaim old namespaces deliberately,
list them (`budget-report.sh status` → `namespaces`) and purge by name.

## Changing the limit

1. Edit `KVP_CACHE_BUDGET_GB` in `configs/generic/cache.env.local`.
2. `bash scripts/stop-cache.sh && bash scripts/start-cache.sh` (data kept).
3. Verify: `bash scripts/budget-report.sh status`.

Lowering the limit below current usage: new stores are rejected immediately
(hard cap); the sweep evicts down to the new target on the next cycle.