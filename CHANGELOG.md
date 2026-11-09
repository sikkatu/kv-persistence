# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 74 (hardware run, step 1: NUMA_MAP validator + honest promotion NOTE)
* HOTSPLIT_NUMA_MAP validator rejected the CORRECT value (hardware-run
  blocker): the regex demanded `node:cpu,cpu;node:cpu`
  ('0:0,1,2,3;1:4,5,6,7') while the real consumer
  (`patches/numa_bind.py map_from_env`) parses `gpu:node` pairs joined by
  commas -- exactly the format `tp4.env.example` documents
  ('0:0,1:0,2:1,3:1'). A correct map aborted the launch with "nothing was
  started". The validator now accepts the real format (strictly) and the
  error message shows it. Regression test added.
* The promotion NOTE lied about `auto`: "engine, namespace and gate will
  all use KV_CACHE_DTYPE=auto" is misleading -- `auto` resolves to the
  MODEL's dtype from config.json BEFORE the engine starts and BEFORE the
  namespace is hashed (for a bf16 model it equals the pinned bfloat16 --
  the SAME cache namespace). The NOTE now explains the resolution.
## Round 73 (namespace-override consent, HOT_GIB equality, negative dtype case)
* SECURITY (review): `KVP_MODEL_NAME` from the environment is the same
  escape hatch as the recipe's `KV_PERSIST_NAMESPACE`, but a random export
  would silently redirect the cache to a FOREIGN namespace -- and the
  namespace IS the model-separation guard. The override now requires
  EXPLICIT consent (`KVP_ALLOW_NAMESPACE_OVERRIDE=1`) and prints a loud
  WARNING both when used and when an un-consented value is IGNORED
  (identity-derived name wins). The test passes the consent flag -- that is
  what the hook is for.
* HOTSPLIT_HOT_GIB is COMPARED for equality on both paths (review: it was
  allow-listed as a permitted difference -- the recipe default could drift
  unnoticed). The parity test reproduces the delegated setup (one shared
  export) and asserts BOTH docker runs carry the SAME value (30).
* The dtype test now describes the negative boundary (review): when
  `config.json` declares NO dtype (no `torch_dtype`, no `dtype`), `auto`
  CANNOT resolve -- it stays `auto` and hashes to a DIFFERENT namespace
  than the pinned `bfloat16`. That is a SAFE cache miss, never a wrong
  read; the pin is a no-op only for a model whose config declares its
  dtype.
## Round 72 (behavioural dtype test, full persist-ON parity with an allowlist)
* KV_CACHE_DTYPE -- the SUBSTITUTION itself (review: the old test only
  checked a comment): `tests/test_engine_delegation.py` now runs the REAL
  `resolve_effective_identity` against a fake `config.json`
  (`torch_dtype: bfloat16`) -- `auto` and the pinned `bfloat16` resolve to
  the SAME value and therefore the SAME cache namespace (real
  `CacheIdentity`), while `fp8` lands in its own. The pin cannot move the
  warmed cache's namespace -- verified behaviourally, not by comment.
* Launcher parity with persistence ON (review step-2 item 5):
  `tests/test_launcher_parity.py::test_full_docker_run_parity_with_persist`
  runs BOTH launchers with the connector attached and compares the WHOLE
  `docker run` token-by-token against an EXPLICIT allowlist (container
  names, env-file /live paths, root vs same-UID caches, explicit HOTSPLIT
  defaults). The reviewer's measured difference list is pinned as data; any
  drift outside it fails the test. Required flags on BOTH sides:
  `--kv-transfer-config`, `--block-size`, `--enable-prompt-tokens-details`.
* The kv launcher honours `KVP_MODEL_NAME` from the environment (the exact
  analogue of the recipe's `KV_PERSIST_NAMESPACE`) so both paths run under
  ONE pinned namespace -- the parity hook; an explicit override stays an
  operator decision (documented escape hatch), never a default.
> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
