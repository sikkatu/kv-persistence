# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。
## 第 79 轮（recipe 路径 same-UID 回归修复、recovery 测试 fail-fast + 诊断）
* **反复出现的 `register_kv_caches` 300 秒超时的根源已在硬件上确认**（审阅者 +
  驱动日志）：委派的 recipe 路径以 ROOT 运行引擎，而主机上的缓存服务以普通用户
  运行——正是这种 UID 错配导致 `CUDA_ERROR_MAP_FAILED`（"Failed to
  open/create interprocess memory file / remote IPC semaphore pool" = EACCES，
  /dev/shm 中 root 所有的 `cuda.shm.*`）。第 45h/48 轮的 same-UID 契约在委派时
  丢失——与第 76/77/78 轮同类缺陷。
* `run-tp4-hotsplit.sh` 现在**默认**以 `$(id -u):$(id -g)` 运行引擎
  （`KVP_SAME_UID=1`，退出用 `KVP_SAME_UID=0`），HOME / HF / XDG / TRITON
  缓存放在 `/tmp`——与 `run-vllm-with-persist.sh` 完全一致。
* recipe 路径的 API 等待现在按缓存服务日志中的新鲜 `Error handling operation`
  **fail-fast**（打印根因；标记在 `docker run` 之前采集，秒级故障也能捕获），
  而不是干等引擎的 300 秒超时。
* `test-recovery.sh`：`api_ready` 同样 fail-fast，并在容器死亡时打印
  `docker logs`（无需手动取证）；场景 B 在 `docker start` 之前运行 CUDA IPC
  预检（正是 B 倒下的那一步）；服务日志的所有读取都用 `grep -a`（NUL 字节会让
  普通 grep 静默跳过匹配）。
* `start-all.sh` 向共享预检传递 same-UID 标志（预检必须验证引擎实际使用的边界）。
* 回归测试：`tests/unit/test_round79_recovery_failfast.py`（kv，5 个测试）+
  `tests/test_round79_same_uid.py`（recipes，3 个测试）——在旧代码上为红色。
  parity allowlist 已更新：`--user` 和 `/tmp` 缓存挂载现在是共享 token（两条
  路径都 same-UID），不再是单侧差异。



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
