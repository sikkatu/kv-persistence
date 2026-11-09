# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 76 轮（激活检查的假 SIGPIPE、共享配置校验、TP 覆盖）
* 激活检查在大引擎日志上说谎（审阅，硬件运行复现）：
  `printf '%s' "$log" | grep -q ...` 在 `set -o pipefail` 下——`grep -q`
  在首个匹配处退出并关闭管道，`printf` 收到 SIGPIPE（141），流水线
  “失败”，`if` 走“未找到”分支，尽管日志里确实有标记。17 分钟模型加载
  日志上四个标记全部读作 [MISSING]，而引擎正常——同样的管道还守卫错误
  检查，真正的错误可能被当作成功。8 处（每个启动器 4 处）现在使用
  here-string（`grep -q -- "..." <<<"$log"`）——无管道、无 SIGPIPE。
  100 KiB 日志的回归测试覆盖两个方向（有标记 → VERIFIED；标记/错误的
  正确组合 → 正确结论）。
* HotSplit 配置校验移入共享模块
  （`hotsplit_env.sh::_hotsplit_validate_config`）：这些检查
  （NUMA_MAP 格式、DYN_DECAY 0..1、DYN_MARGIN ≥ 1、DYN_MAX_SWAPS 1..1024、
  整数与浮点旋钮）此前只在 kv 启动器里——切换引擎路径到 recipe 后它们会
  消失。两个启动器现在调用同一函数。
* 新检查（审阅）：HOTSPLIT_NUMA_MAP 必须覆盖每个 GPU 序号 0..TP-1。
  `numa_bind.py` 对缺失的 GPU 静默返回 -1，却仍报告 `numa_bind=ACTIVE`
  ——"0:0,1:0" 在 TP4 上绑定了 2/4 个 GPU 且看起来健康。校验器现在
  指出每个缺失的序号。行为测试（TP4/TP2）在
  `tests/test_hotsplit_validation.py`——切换默认到 recipe 启动器的条件。
* namespace 覆盖同意（第 73 轮）现已覆盖：
  `tests/test_namespace_override_consent.py`——没有
  `KVP_ALLOW_NAMESPACE_OVERRIDE=1` 时继承的 `KVP_MODEL_NAME` 被忽略并
  打印醒目 WARNING（identity 派生名优先）；带标志时覆盖生效，仍打印
  WARNING。
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
> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
