> 🌐 Language versions: **English** | [Русский](security.ru.md) | [中文](security.zh.md)

# Security and data handling

## Threat model

Persistent KV data DOES contain information recoverable from the original
prompts (with enough effort one can invert KV activations toward prompt content;
treat the cache root as if it contained the conversation text itself).

## Controls

| Control | Implementation |
|---|---|
| Filesystem permissions | `KVP_CACHE_ROOT` created `chmod 700`; scripts never loosen it |
| Management endpoint exposure | LMCache HTTP frontend (`--http-host`/port from config) bind **127.0.0.1** only; no proxying without auth (docs/troubleshooting.md if you must reach it remotely) |
| Cache request port | `127.0.0.1:5555` ZMQ, loopback only |
| Credentials | never in repository or cache config; `live/api_key` (gitignored, chmod 600) holds the vLLM API key exactly like the production recipe does |
| Diagnostics logging | scripts and admin CLI log keys, sizes and counts only — never prompt text, headers or tokens |
| Cache isolation | compatibility namespaces make cross-model/cross-configuration reads impossible (`docs/architecture.md`) |
| Deletion safety | `kvp-admin purge` refuses any path outside `KVP_CACHE_ROOT/*kvp-*` (exit 2) and requires `--yes` |
| Shared storage | NOT supported as the cache root (preflight warns if the filesystem is a network mount) — do not put this on NFS/CIFS/shared NVMe |
| Startup wipe | never happens: preserved across restarts and upgrades |
| Clearing data explicitly | `bash scripts/budget-report.sh purge --all --yes` (manual only) |

## Rules you should keep

* Do not run the lmcache HTTP frontend behind an unauthenticated reverse proxy.
* Do not copy `KVP_CACHE_ROOT` between machines (namespaces embed host-independent
  model identity — but permissions and secret-derived content do not travel).
* Do not add prompt text to logs while debugging; add keys/counts instead (the
  `prompt_probe.py` tool follows this rule).
* Treat value-per-namespace stats as metadata about users' prompts (lengths/dates)
  and keep access to `budget-report.sh status` output as private as the cache.

## Reporting security issues

Use the private repository channel (sikkatu/kv-persistence issues), not public
disclosure, until the report is triaged.