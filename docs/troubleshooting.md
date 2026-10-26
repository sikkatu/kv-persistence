> 🌐 Language versions: **English** | [Русский](troubleshooting.ru.md) | [中文](troubleshooting.zh.md)

# Troubleshooting

Quick checks first:
```bash
bash scripts/budget-report.sh status    # usage, namespaces, disk free
tail -n 100 "$KVP_CACHE_ROOT/lmcache-server.log"   # cache service
docker logs --tail 100 mimo-pro-tp4-kvp            # engine
```
(`KVP_CACHE_ROOT` comes from `configs/generic/cache.env.local`, default
`/var/lib/kv-persistence/l2`.)

## Preflight fails: identity fields

`[!!] KVP_MODEL_REVISION must be a full 40-hex SHA` — the accepted value is EITHER
of two clearly distinct formats (choose one per deployment):

1. **Local file fingerprint** (what `setup-identity.sh` writes by default): a
   SHA-256 digest of config.json + *.index.json + every shard's CONTENT bytes.
   NOT a commit SHA, but it identifies the files actually on disk byte-for-byte.
   Regenerate it by rerunning `scripts/setup-identity.sh <model-dir>`.
2. **Upstream HF commit SHA**, only if you deliberately track remote provenance:
   ```bash
   curl -fsSL https://huggingface.co/api/models/XiaomiMiMo/MiMo-V2.6-Pro-MOPD \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
   ```
   Warning: this is the HEAD of the remote branch — NOT necessarily the revision
   of your locally downloaded files (use it ONLY after verifying the local
   download actually came from that commit).

Both write into `configs/mimo-v2.6-pro/identity.env.local` (site override,
gitignored); the tracked `identity.env` stays unmodified for a clean `git pull`.
A branch name is never accepted (40-hex only).

`[!!] KVP_CHAT_TEMPLATE_DIGEST must be 64 hex` — run preflight with
`KVP_MODEL_DIR=$HOME/models/MiMo-V2.6-Pro-MOPD bash scripts/preflight.sh`; it
prints the digest to copy into `identity.env`.

## lmcache CLI missing / ABI mismatch

`ERROR: lmcache CLI missing` — install per docs/deployment.md §3. If pip wheels
fail to `import lmcache` (undefined symbol errors), the wheel ABI does not match
the PyTorch/CUDA stack of the production image; run `lmcache server` from the
same pinned image instead (`--entrypoint lmcache`), keeping the same arguments.

## Cache service starts but vLLM cannot connect (MP mode)

Symptom: vLLM logs show connector errors / retries against port 5555.
1. `bash scripts/start-cache.sh` must run BEFORE `run-vllm-with-persist.sh`
   (the launcher refuses otherwise).
2. `grep -n "error\\|traceback" "$KVP_CACHE_ROOT/lmcache-server.log" | tail`.
3. Both processes must share the same host network namespace — do not move the
   server to another port/host without updating both sides
   (`lmcache.mp.host`/`lmcache.mp.port` in `run-vllm-with-persist.sh`).

## No cache hit after vLLM restart (expected TTFT drop missing)

Verify in order:
1. `cached_tokens` in the request response > 0 (the engine saw cache hits).
2. `grep -c Retrieved "$KVP_CACHE_ROOT/lmcache-server.log"` increases during the
   request; entries should mention L2/disk, not only L1.
3. Cache bytes on NVMe did not grow to rewrite the same data
   (`budget-report.sh status` before/after).
4. Identity unchanged: run `scripts/preflight.sh` — a changed revision/dtype/
   template silently moves you to a NEW namespace (by design).
5. `KVP_PERSIST_ENABLED` must be `true` (start-cache.sh warns otherwise).

## Disk full / rejected stores

`budget-report.sh status` shows `free_bytes: 0` or the adapter logs rejection at
the hard cap — raise `KVP_CACHE_BUDGET_GB` (respect the pinned device's free space) or run
`budget-report.sh purge --namespace … --yes` on old namespaces. Never delete
files manually inside the cache root while the service runs.

## Ports / reachability (remote client)

For a chat client on another machine, the ENGINE port (default 30007) must be allow-
listed (e.g. ufw rule for your VPN subnet + `BIND`/`API_KEY` rules from the
production recipe). The cache's HTTP frontend (8080) is deliberately loopback
only — do NOT expose it; use SSH port forwarding for remote diagnostics:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@host     # inspect, do not publish
```

## Eviction fires too aggressively

Raise `KVP_EVICT_TRIGGER_WATERMARK` (soft threshold) or lower
`KVP_EVICT_RATIO`; verify with a small isolated budget first (docs/eviction-
and-deduplication.md). Guessed tuning values will not fix policy bugs — see
`kvp-admin status` `evictions`/`reclaimed_bytes` counters for the actual regime.

## Everything is slow and reuses little

Measure what part of the prompt is actually stable: run
`python3 implementation/prompt_probe.py --tokenizer "$MODEL_DIR" --request-file
a.json --request-file b.json` with two REAL request bodies exported from your chat client.
If `stable_prefix_tokens` is small, caching cannot help (timestamps/tool ids may
differ); adjust serialization or accept the limitation — do not force caching
by truncating history (forbidden by the project rules).

## Recovery of a corrupted cache file

Corruption is tolerated (lookups fall through to recompute). To clean: stop the
service and `purge --all --yes`, then start again — this is the documented
explicit cleanup, never automatic.