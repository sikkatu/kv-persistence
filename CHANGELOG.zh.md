# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 78 轮（CONTAINER= 覆盖、API_KEY 规范文件契约、诚实的 parity、完整 exit-code 链）
* 环境中的 `CONTAINER=` 在 recipe 路径上被忽略（start-all.sh 用默认值覆盖它）。
  现在的优先级：`CONTAINER` > `KVP_ENGINE_CONTAINER` > 默认值——生效名称写入
  `live/engine_container` 并导出给 recipe 启动器。
* 修复 `run-tp4-hotsplit.sh` 中的 `API_KEY` 脑裂：env 密钥用于启动，而
  `~/.config/kvp/api_key` 仍保存旧密钥（`live/api_key` 镜像新密钥）——同时存在
  两个不同的密钥。契约（与 kv 路径相同）：规范文件是唯一事实来源；显式 env
  `API_KEY` 胜出并带响亮 WARNING 保存到规范文件。
* 全量 parity 测试曾用 `set()` 比较 docker-run 的 token——重复的标志/挂载和
  次数都不可见。现在用 Counter（多重集）+ 单次使用标志的重复检测 + 挂载目标
  多重集比较（标志的顺序诚实地不在范围内：两个启动器构建命令的顺序不同，
  docker 会忽略它）。
* exit-code 现在通过完整链（`kvp up` -> `exec` -> `start-all.sh` -> 前台 recipe）
  用任意非零码验证，而不只是经 start-all.sh 的 exit 1。
## 第 77 轮（两条引擎路径共用预检、ipc-test 标签改为诚实）
* lmcache RPC 探测（`mp_ping`）和 CUDA IPC 边界测试（`ipc-test.sh`）此前只接在
  kv 启动器里 —— 委派的配方路径（`KVP_ENGINE=recipe`）加载 130 个分片约 15 分钟，
  然后死在 `register_kv_cache`（`cuIpcOpenEventHandle` 抛出 CUDA_ERROR_MAP_FAILED），
  加载前没有任何检查。与第 76 轮同一类缺陷（只存在于一个启动器里的检查，在切换
  默认路径时会消失）。现在由一个共享函数 `env.sh::_kvp_pre_flights` 在两条路径的
  模型加载前运行；预检失败即拒绝启动（“nothing was started”），并给出诚实的修复
  方式：把缓存服务跑在容器里（`KVP_LAUNCH_MODE=docker`）或以引擎的 UID 运行
  （`KVP_SAME_UID=1`）。诚实的适用范围说明：预检只能发现启动时的死通道，不能发现
  加载中途服务死亡（启动器仍会对服务端错误 fail-fast）。
* `ipc-test.sh` 的标签不再撒谎：不论 `PRODUCER_USER` 是什么（包括 `0:0`，即 root
  生产者）都会打印 “same-UID test -- no root”。现在 root 生产者标记为
  “root producer” 并给出自己的 PASS 判定（“ROOT-PRODUCER result”）。回归测试
  完整复现（`tests/unit/test_round77_shared_preflights.py`，8 个测试，在旧代码
  上为红色）。
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
