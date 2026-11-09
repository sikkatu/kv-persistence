# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 74 轮（硬件运行，步骤 1：NUMA_MAP 校验器 + 诚实的提升提示）
* HOTSPLIT_NUMA_MAP 校验器拒绝了正确的值（硬件运行阻塞）：正则要求
  `node:cpu,cpu;node:cpu`（'0:0,1,2,3;1:4,5,6,7'），而真正的消费方
  （`patches/numa_bind.py map_from_env`）解析的是逗号分隔的 `gpu:node`
  对——正是 `tp4.env.example` 记载的格式（'0:0,1:0,2:1,3:1'）。正确的映射
  以 “nothing was started” 中止启动。校验器现在接受真实格式（严格），
  错误信息也展示该格式。已添加回归测试。
* 提升提示对 `auto` 说谎：“engine, namespace and gate will all use
  KV_CACHE_DTYPE=auto” 有误导性——`auto` 在引擎启动之前、namespace 哈希
  之前就从 config.json 解析为模型的 dtype（对 bf16 模型等于固定的
  bfloat16——相同的缓存 namespace）。现在提示解释该解析过程。
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
> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
