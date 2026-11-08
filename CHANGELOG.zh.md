# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


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
## 第 70 轮（委托：KVP_ENGINE=recipe，默认关闭）

* 一个引擎启动，两条路径：`start-all.sh` 现在支持 `KVP_ENGINE`
  （`kv` — kv 启动器，默认；`recipe` — 经硬件验证的
  `run-tp4-hotsplit.sh`，带 KV_PERSIST=1）。默认关闭，旧引擎代码在硬件
  运行确认 recipe 路径之前不受影响——完全符合审阅要求。
* 审阅者的步骤 2 清单，全部接入并测试：
  * 没有标志时行为不变（docker run 契约快照固定行为；默认路径测试断言
    运行的是 kv 启动器）；
  * 单一容器名：`start-all` 将有效名称记录到
    `live/engine_container`；`stop-engine.sh`（kvp down / stop-all.sh）
    和 `kvp logs/status` 读取它——停止的正是启动的那个（recipe 将容器
    命名为 mimo-pro-tp4-hotsplit，kv 启动器为 mimo-pro-tp4-kvp）；
  * 单一密钥来源：共享的 `~/.config/kvp/api_key` 以 API_KEY 导出到
    环境变量——绝不进入 argv——recipe 启动器复用它且不会生成第二个；
  * KV_CACHE_DTYPE：固定为 bfloat16（recipe 默认 auto）。差异在测试中
    明确展示，绝不被规范化隐藏；identity 哈希考虑 dtype（每种 dtype
    -> 独立命名空间；auto->bfloat16 解析契约在启动器源码中断言）；
  * 退出码：recipe 启动器在前台运行——它的 exit 1（激活检查）原样传到
    start-all.sh / kvp up（已测试：缺少标记 -> exit 1）。
* `tests/test_engine_delegation.py`（6 个测试）覆盖清单；强制的
  identity-gate 被桩替换（与 test_kv_persist_behaviour 相同的装置）——
  真正的 identity 属于 kv-persistence 的领域。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
