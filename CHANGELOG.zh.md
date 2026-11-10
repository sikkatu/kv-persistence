# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。
## 第 80b 轮（legacy kv 启动器已删除；所有检查均已保留）
* **`run-vllm-with-persist.sh` 已删除**——`start-all.sh` 现在只有一条引擎
  路径（`KVP_ENGINE=recipe`）。`KVP_ENGINE=kv` 诚实地报错退出（没有任何
  东西丢失：共享检查都在 `env.sh` / `hotsplit_env.sh` 中）。
* **旧启动器拥有的每一项检查都经过审计并保留**——三项预检（TCP 可达、
  L2 编解码器匹配、lmcache 版本匹配）、引擎死亡时的缓存服务诊断、GPU
  预检 + 缓存服务 PID 检查、`engine-identity.env` 写入、以及 namespace
  覆盖 consent 门（第 72 轮）全部迁移到 recipe 启动器 / 共享的
  `_kvp_pre_flights`。它们曾经只存在于被删除的脚本中（第 76 轮的
  缺陷类别）。
* 22 个 kv + 5 个 recipes 测试文件已迁移：文本契约检查读取契约并集
  （`_targets.contract_text()` / `_contract.py`），行为测试驱动 recipe
  启动器。Parity 测试（两个启动器）变为引擎 docker 契约测试（一个）。
* kv 378+20，recipes 388+5——全部绿色。

## 第 80 轮（默认引擎 = recipe，硬件验证已通过）
* **硬件运行于 2026-11-10 完整通过**（`test-recovery.sh`
  `VERDICT: PASS`，exit 0）：recipe 路径上的 same-UID 引擎（1000:1000），
  场景 A（仅重启引擎）memory/L1 命中，`cached_tokens: 2560`，TTFT 2.44 秒
  对比冷启动 11.49 秒；场景 B（完全重启）磁盘恢复，带 L2 读取事件和
  缓存服务进程读取的 411 MB。
* **`KVP_ENGINE=recipe` 现在是默认值**（`start-all.sh`）。旧的 kv 启动器
  仅作为显式 legacy 路径 `KVP_ENGINE=kv` 保留到第 80 轮清理将其删除；
  所有共享检查都在 `env.sh` 中，两条路径都会运行。

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



