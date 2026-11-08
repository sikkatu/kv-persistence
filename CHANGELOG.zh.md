# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 61 轮（cross-repo CI 路径、布尔等价性、兼容性探测）

* recipes CI 中的跨仓库契约测试现在实际执行：测试首先在 workspace 内部查找
  kv-persistence checkout（`RECIPES/kv-persistence`，GitHub Actions 布局），
  然后才回退到开发者主目录路径。六个契约测试不再在 CI 中跳过。
* KV 启动器中的布尔 HotSplit 参数与 recipe 启动器完全一致地归一化
  （`normalize_bool`：true/yes/on -> 1，false/no/off -> 0），在 source 共享
  模块之前执行。HotSplit 组件与字符串 "1" 比较（`HOTSPLIT_NUMA == "1"`），
  因此原始的 `true` 会静默禁用功能。覆盖 DYNAMIC / DRYRUN / NUMA /
  ALLOW_STATIC_FALLBACK / STRICT_HOOK / NUMA_STRICT / DYN_LOG。
* KV 启动器现在执行与 recipe 启动器相同 model-补丁兼容性探测（base_loader
  接缝、model_loader.utils 定义、mimo_v2/mimo_v2_omni 的 `load_weights`），
  在挂载替换文件之前执行，并记录被遮蔽原文件的 provenance SHA-256。镜像
  digest 变化会在 13-16 分钟模型加载之前被捕获，而不是之后。
* 验证完整性（P2）：`HOTSPLIT_HOST_FLOOR_GIB` 和 `HOTSPLIT_VRAM_RESERVE_GIB`
  作为非负浮点数验证；整数 knobs 带范围（DYN_STEPS/TOPK/LIVE_SECS/
  DECODE_MAX/错误限制 >= 1，DYN_MAX_SWAPS 1..1024 —— 计划代码自身的边界）；
  MAX_TOKENS / DYN_PRIOR_TOKENS 允许 0。
* 测试计数诚实性：sandbox 运行（346+2 / 338+5）与 CI 运行（344+4 / 330+13）
  不同，因为 CI 缺少可选依赖（wiki checkout、lz4 绑定）。文档引用 CI 数字；
  两套测试均通过。

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

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
