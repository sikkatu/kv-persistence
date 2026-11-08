# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 63 轮（解析器一致性、密钥不进 ps、/live 挂载、kvp CLI）

* 引擎启动器中的 `_tp4_seed` 现在与 recipe 的解析器逐字节一致：`$HOME`/
  `${HOME}` 被展开，外围引号被去掉，行内注释只在 " #" 处截断，没有有效
  标识符的行（带缩进的注释）被跳过而不是以 "invalid variable name" 崩溃。
* API 密钥不再出现在 `ps` 输出中（全部 5 个 recipe 启动器、KV 启动器和
  test-recovery.sh）：通过 `docker --env-file`（chmod 600）和 `curl -K`
  配置文件传递，而不是 `-e VLLM_API_KEY=<value>` 和
  `-H "Authorization: Bearer ..."` 命令行参数。
* KV 启动器挂载 `/live`（`$ROOT/live:/live`）以便路由计数器持久化；激活
  检查现在也会在 "live snapshot failed" 时失败，并在启用 NUMA 绑定时验证
  `HOOK STATUS numa_bind=ACTIVE`。
* 密钥迁移现在真正写入规范文件 `~/.config/kvp/api_key`（`_kvp_load_api_key`
  中的 "migrate below" 承诺从未实现——两个仓库一直在用不同的密钥）。
* tp4.env 中的 `USE_LOSSLESS_SERDE`/`LMCACHE_L2_SERDE` 现在能到达统一的
  编解码决策（start-all 在解析之前先 seed tp4.env），用户提供的
  `VLLM_EXTRA_ARGS` 不再悄悄移除 MiMo 解析器（关闭：`KVP_MIMO_PARSERS=0`）。
* kv-persistence 中添加了 `KVP_COMPAT_VERSION` 钉定文件（recipe 启动器
  已经在检查它）；`KVP_REPO`/`KVP_REPO_ROOT` 两种拼写都被接受。
* PATH 上的 `kvp` CLI（`scripts/install-kvp.sh` -> `~/.local/bin/kvp`）：
  `kvp up|down|status|logs`，仓库位置记忆在 `~/.config/kvp/`。
* 修正过时文档（README EN/RU/ZH + lmcache-server.env 注释）：省略编解码器
  变量得到无损 zstd，raw 需要显式 `LMCACHE_L2_SERDE=none`。
* 测试：recipe 启动器测试现在遵循 env-file 契约（密钥不应是命令行参数）
  并考虑 `~/recipes` 查找布局。

## 第 62 轮（浮点参数范围 + 新 CI 计数）

* 浮点参数验证现在在主机上强制执行计划代码的真实范围：
  `HOTSPLIT_DYN_DECAY` 必须是 0..1，`HOTSPLIT_DYN_MARGIN` >= 1
  （hotsplit.py 的 `_dyn_float` 边界）。之前只检查「是否为数字」，因此
  `DYN_DECAY=2` / `DYN_MARGIN=0.5` 能通过主机验证，在模型加载后才被容器内
  拒绝。范围计算使用纯 bash（无外部工具，无 fail-open）。
* 修复 cross-repo 路径后刷新 CI 计数：recipes CI 现在是 **336 passed +
  7 skipped**（第 61 轮注释中为 330+13）；kv-persistence CI 不变 ——
  344 passed + 4 skipped。文档引用当前数字。

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
* 测试计数诚实性：sandbox 运行（346+2 / 338+5）与 CI 运行（344+4 / 336+7）
  不同，因为 CI 缺少可选依赖（wiki checkout、lz4 绑定）。文档引用 CI 数字；
  两套测试均通过。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
