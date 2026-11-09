# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


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
## Round 71 (delegation defects: hot bank, engine_container record, fresh-machine key)
* The delegated path no longer silently disables the hot experts (review
  probe A): `HOTSPLIT_HOT_GIB` has ONE default (30 GiB) in
  `scripts/env.sh` (`_kvp_hotsplit_hot_gib_default`) taken by BOTH engine
  paths, and `start-all.sh` refuses to launch when it resolves empty. The
  kv path used to set 30 while the recipe path passed nothing -- the recipe
  launcher warned "the split is a no-op" and ran with an EMPTY hot set.
* ONE container-name record (review probe B): BOTH branches write the
  effective name to `live/engine_container`. A recipe run followed by a kv
  run used to leave the recipe name behind -- `stop-engine.sh` /
  `kvp down|logs|status` stopped the wrong container and the live kv engine
  kept running.
* Fresh-machine first launch generates the key on BOTH paths (review probe
  C): the generation contract (`_kvp_gen_key` + `_kvp_ensure_api_key`) lives
  in `scripts/env.sh` and is called by the kv launcher AND the delegated
  recipe branch (the recipe path used to die with "no API key found").
  Security contract unchanged: auto-generation is loopback-only, a
  non-loopback bind ALWAYS requires an explicit key.
* The reviewer's probe file `test_review_round70_probes.py` (tests A/B/C) is
  part of the recipes suite and passes against these fixes.
> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
