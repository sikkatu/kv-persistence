# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 73 轮（namespace 覆盖需同意、HOT_GIB 等值比较、dtype 负例）
* 安全（审阅）：环境变量 `KVP_MODEL_NAME` 与 recipe 的
  `KV_PERSIST_NAMESPACE` 同为逃生口，但随手 export 会把缓存静默转移到
  外来 namespace——而 namespace 正是模型隔离的保护。现在覆盖需要显式同意
  （`KVP_ALLOW_NAMESPACE_OVERRIDE=1`），并且无论实际使用还是未同意值被
  忽略（identity 派生名优先）都会打印醒目 WARNING。测试会传入同意标志
  ——钩子就是为此存在的。
* HOTSPLIT_HOT_GIB 在两条路径上按等值比较（审阅：此前它被列入允许差异
  ——recipe 默认值可能悄悄漂移）。parity 测试复现委托部署（一次共享
  export），断言两个 docker run 携带相同值（30）。
* dtype 测试现在描述负例边界（审阅）：当 `config.json` 未声明 dtype
  （无 `torch_dtype`、无 `dtype`）时 `auto` 无法解析——保持 `auto`，哈希出
  与固定 `bfloat16` 不同的 namespace。这是安全的缓存未命中，绝非错误读取；
  该固定仅对声明了 dtype 的模型是 no-op。
## 第 72 轮（行为级 dtype 测试、带 allowlist 的完整持久化 parity）
* KV_CACHE_DTYPE——替换本身（审阅：旧测试只检查注释）：
  `tests/test_engine_delegation.py` 现在对伪造 `config.json`
  （`torch_dtype: bfloat16`）运行真正的 `resolve_effective_identity`：
  `auto` 与固定的 `bfloat16` 解析出相同的值、因而相同的缓存 namespace
  （真实 `CacheIdentity`），而 `fp8` 有自己的。固定值不会移动已预热缓存的
  namespace——通过行为验证，而非注释。
* 持久化开启下的启动器 parity（清单第 5 项）：
  `tests/test_launcher_parity.py::test_full_docker_run_parity_with_persist`
  在连接器挂载状态下运行两个启动器，逐 token 比较整个 `docker run`，
  并使用显式 allowlist（容器名、env-file /live 路径、root 与 same-UID
  缓存、显式 HOTSPLIT 默认值）。审阅者测得的差异列表被固定为数据；
  超出清单的任何差异都会使测试失败。两侧必备参数：
  `--kv-transfer-config`、`--block-size`、`--enable-prompt-tokens-details`。
* kv 启动器支持环境变量 `KVP_MODEL_NAME`（recipe 的 `KV_PERSIST_NAMESPACE`
  的精确对应物）——两条路径在同一固定 namespace 下运行（parity 钩子）；
  显式覆盖始终是操作员的决定（已文档化的逃生口），绝不作为默认值。
## 第 71 轮（委托缺陷：hot bank、engine_container 记录、首次启动密钥）
* 委托路径不再静默禁用热专家（探针 A）：`HOTSPLIT_HOT_GIB` 在
  `scripts/env.sh` 中只有一个默认值（30 GiB，`_kvp_hotsplit_hot_gib_default`），
  两条引擎路径都取它；解析为空时 `start-all.sh` 拒绝启动。此前 kv 路径设 30，
  而 recipe 路径什么都不传——recipe 启动器警告 “split is a no-op”，
  并以空热集运行。
* 唯一的容器名记录（探针 B）：两个分支都把有效名称写入
  `live/engine_container`。此前 recipe 运行后接 kv 运行会留下旧名字——
  `stop-engine.sh` / `kvp down|logs|status` 停错了容器，活着的 kv 引擎继续运行。
* 全新机器的首次启动在两条路径上都生成密钥（探针 C）：生成契约
  （`_kvp_gen_key` + `_kvp_ensure_api_key`）位于 `scripts/env.sh`，
  由 kv 启动器和委托的 recipe 分支共同调用（recipe 路径此前以
  “no API key found” 退出）。安全契约不变：自动生成仅限 loopback，
  非 loopback 绑定始终要求显式密钥。
* 审阅者的探针文件 `test_review_round70_probes.py`（测试 A/B/C）
  已纳入 recipes 测试集，针对这些修复全部通过。
> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
