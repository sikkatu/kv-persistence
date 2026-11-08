# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 60 轮（CI 模块路径、BOOT_HOOK 等价性、完整验证）

* kv-persistence CI 现在检出 `sikkatu/recipes`（路径 `recipes`），测试辅助
  函数首先相对于 checkout 解析共享模块 `hotsplit_env.sh`（开发者主目录
  路径作为后备）—— 每次 CI 运行都失败的两个测试现在读取真实模块。
* `HOTSPLIT_BOOT_HOOK` 不再强制为 1：挂载模型补丁后，替换的
  `mimo_v2_hotsplit.py` 已经调用 `hotsplit.apply_hotsplit`，因此启动器
  默认 boot hook = 0 —— 与 recipe 启动器的决定相同（同一加载器不会有双重
  包装）。激活健康检查遵循配置值：`boot_hook=INACTIVE` 在 0 时是正确状态。
* 主机端 HotSplit 验证现在覆盖共享模块传递的所有数值 knobs：
  DYN_STEPS、DYN_MAX_SWAPS、DYN_PRIOR_TOKENS、MAX_TOKENS、TOPK、
  LIVE_DECODE_MAX、DYN_MAX_PLAN_ERRORS、DYN_MAX_COMMIT_ERRORS、
  VRAM_RESERVE_GIB、LIVE_SECS（后六项之前未验证）。回归测试锁定全部三个
  修复。

## 第 59 轮（HotSplit 启动器等价性 + CI/契约修复）

* kv-persistence 引擎启动器现在运行完整的、经硬件验证的 HotSplit 配置
  （P1）：`--cap-add IPC_LOCK/SYS_NICE`、`--compilation-config FULL_DECODE_ONLY`
  和完整的 `HOTSPLIT_*` 环境（NUMA_MAP、LIVE_COUNTS/SECS、DYN_STEPS/
  MAX_SWAPS/MARGIN/DECAY/PRIOR_TOKENS、MAX_TOKENS 等）不再缺失。一个共享
  模块（recipe 中的 `scripts/hotsplit_env.sh`）为两个启动器构建环境和
  docker 参数——不再有人工同步的重复列表。
* 跨仓库契约测试在 CI 中实际运行（P1）：workflow 现在额外检出
  kv-persistence，因此 6 个契约测试比较真实文件而不是跳过。
* recipes 委托器（`scripts/start-all.sh`）导出 `RECIPE_DIR` 指向自身的
  recipe 检出（P1）——从任意克隆目录均可启动，不再猜测
  `~/sikkatu-recipes`。
* 引擎启动器的头部注释不再声称「仅 UVA BASELINE」（P2）——现在描述实际
  的默认 hotsplit 行为。
* HotSplit 配置的主机端验证在 13-16 分钟模型加载之前运行（无效的
  HOT_GIB / DYN_* / NUMA_MAP 立即失败），`--v1/models` 响应后运行强制的
  激活健康检查：boot hook ACTIVE、dynamic hook 已安装、live 计数器存在、
  无空 bank 或事务错误。仅 HTTP 200 不再被视为热专家工作的证明。

## 第 58 轮（统一配置层 + 共享密钥回归修复）

* 共享 API 密钥回归已修复（recipes）：启动器现在每次启动都将规范文件
  `$HOME/.config/kvp/api_key` 镜像到 `live/api_key`，因此 `manage.sh` /
  `check-tp4-api.sh` 不会在密钥存在时报「需要密钥」。全部五个配方启动器
  使用同一密钥架构；辅助脚本优先读取规范文件且不再依赖 `tr`/`head`
  （纯 bash 读取）。
* 统一编解码决策：`start-all.sh` 只解析一次有效 L2 编解码（共享折叠），
  并将同一值传给缓存和引擎——identity.env 中的编解码钉定不会再让两侧
  使用不同编解码。
* 统一配置层：`start-all.sh` 在任何决策之前加载 cache.env /
  lmcache-server.env / identity.env——缓存 PID 探测使用有效的
  `KVP_CACHE_ROOT`，自定义路径上的缓存会被识别为正在运行。
  `run-vllm-with-persist.sh` 现在读取配方的 `tp4.env`（环境变量优先于
  文件），因此通过 `start-all.sh` 启动时 HOTSPLIT_* / MAX_MODEL_LEN /
  MAX_NUM_SEQS / MOE_BACKEND 能到达引擎。
* 统一入口：recipes 中的 `scripts/start-all.sh` 委托到 kv-persistence
  —— 同一命令可在任何工作目录下运行。`run-tp4-hotsplit.sh` 仍是配方的
  独立启动器。
* 跨仓库契约测试（`tests/test_cross_repo_contract.py`，recipes）：
  编解码语义、lmcache 版本钉定（0.5.6）、端口、namespace 钉定和挂载的
  HotSplit 文件必须在两个仓库中一致——一个仓库的更改不会再悄悄破坏另一个。
* 压缩文档统一（EN/RU/ZH + 配置注释）：无损 zstd 到处是默认值，lz4 是
  备选，逐字节精确原始模式仅为显式关闭（`LMCACHE_L2_SERDE=none`）。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
