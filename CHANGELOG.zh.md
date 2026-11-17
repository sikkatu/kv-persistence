# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **自定义 LMCache 构建**
（基线 `0.5.7.dev428` = `03ee5b82418b6bd44e424cdd46932d9346f43eb2`，
针对上游 PR #4136 与 #5972 的下游补丁，见 `docs/lmcache-build.zh.md`）；
原先固定的 **LMCache 0.5.6** 已被此自定义构建取代。
`requirements-lmcache.txt` 现在指向派生镜像。

### 第 2 轮审阅修复（P0/P1，一项一提交）
* **P1-1** `87b1083`：恢复 r1 重写时丢失的 `nixl==1.5.2`/`openai`/
  `zstandard`/`lz4` 固定（缺 nixl 时 L2 适配器拒启、缺 openai 时 CLI 挂掉）；
  pin 测试改到新契约。
* **P1-2** `95fa1a8`：构建身份烙入镜像（`/etc/kvp-lmcache-identity.json`，
  基线 + 补丁 SHA-256 + wheel SHA-256）并由门禁校验（MISMATCH → 退出 4）；
  `PATCHED_STATE` 哈希 diff 内容；`pytest -rs` 且 skip==失败；三语文档如实。
* **P0-2** `03fe204`：缺 `cuda_ops*.so` 的 wheel 被拒（无 nvcc 的静默
  fallback 正是 cudaMemcpy error 1 的根源）：nvcc 预检、
  `BUILD_WITH_CUDA=1 TORCH_CUDA_ARCH_LIST=12.0`、wheel 内 .so 检查、
  每卡真实 GPU 冒烟（`--gpus all`）、服务器日志 fallback 警告。
* **P0-3** `4fdff85`/`1678964`：reset-persistent-cache.sh 重写 —— 诊断
  log/pid/rendered-json/crash-* 保留（归档到 live/），符号链接缓存根真正
  清空（resolved-path 遍历 + 余留复查），专用缓存目录确认
  （resolved-path 比较，符号链接 KVP_CACHE_ROOT 可用），拒绝网络文件系统，
  侦测活的 host-MP（死 pid 忽略），完整 7.4 报告。9 测试。
* **P0-1** `a259929`：启动时的自定义镜像强制 —— 原生 vLLM 镜像被
  start-cache.sh（默认 `KVP_LAUNCH_MODE=docker`）和两个 recipe 启动器在
  KV_PERSIST=1 时拒绝（镜像标签 vs 补丁 SHA-256，模型加载前执行，
  `verify-image-custom.sh`）；接受本地构建镜像的裸 image-ID
  （`sha256:<64hex>`）细粒度 pin。13 测试。

### LMCache 集成：两个必需的上游修复 + 缓存重置
* 创建 `vendor/patches/lmcache/`：`4136-mp-retrieve-abort.patch`（retrieve/
  abort 竞态：显式 `LOADING` 生命周期、调度器确认的中止传播到 worker、
  无效 store 完成抑制、中止 ID 去重）、`5972-incomplete-kv-objects.patch`
  （窗口下采样后块可用性检查；含 `null_block_id` 的不完整对象在持久化前
  被拒绝，独立完整的组仍被存储），以及 `SOURCES.md`（固定基线提交 +
  各 PR 源码修订 + SHA-256 校验和）。已确认 #6551 已合并入基线
  （通过 `git merge-base --is-ancestor` 验证，无本地重复实现）。
* `scripts/build-lmcache.sh`（新增）：在固定 vLLM 镜像内可重现地构建自定义
  LMCache；验证基线修订与 #6551 祖先关系；确定性地应用两个补丁（无法应用
  或已被意外应用时响亮失败）；运行 #4136 与 #5972 的聚焦上游回归测试；用
  `--no-build-isolation` 针对固定 PyTorch 构建 wheel；将完整源码身份 +
  wheel SHA-256 记录到 `live/lmcache-build/provenance.json`。
* `scripts/build-image.sh`（新增）：从固定 vLLM digest 构建派生镜像，仅安装
  自定义 LMCache wheel；逐位保留 vLLM/PyTorch/CUDA/Python；将最终 digest
  与运行时身份写入 `live/image-identity.env`；此单一镜像同时服务共享 MP
  缓存服务器与所有模型 worker。
* `scripts/reset-persistent-cache.sh`（新增）：唯一的破坏性操作（一次性
  迁移）。要求 `KVP_RESET_CONFIRM=FULL-RESET`；拒绝不安全路径（`/`、
  home、模型、仓库、越界符号链接）；强制/验证先停止服务；写入
  `live/cache-reset-report-<UTC>.txt`。绝不被 start/stop/restart 调用。
* `docs/lmcache-build.{md,ru.md,zh.md}`（新增）：LMCache 构建身份、自定义
  镜像部署、L1/L2 + 缓存身份 + STORE/RETRIEVE 诊断（「STORE_END 不是持久
  写入的证据」）、安全缓存重置流程、缓存未命中诊断，以及在上游合并
  PR #4136/#5972 后移除两个下游补丁的明确未来迁移流程。

### 第 80 轮审阅修复（诚实指标、状态漂移、生命周期）
* `ask()` 指标由 `ttft_s` 更名为 `request_latency_s`（非流式探针测量的是完整
  请求延迟，而非首 token 时间——诚实命名）；
* `start-all.sh` 导出唯一规范 `LIVE_DIR=$ROOT/live`（recipe 的 `$PWD/live`
  默认值在其他目录启动时会漂移）；`live/image` 在 digest 校验之后写入；
* `stop-all.sh` 也停止 observability 栈；`stop-engine.sh` 先 graceful
  `docker stop --time 30` 再 `rm -f`；
* recipe 启动器在直接启动时也运行共享预检（KVP_PREFLIGHTS_DONE 防止重复）；
* 文档清理：所有对已删除 kv 启动器的引用已替换，容器名和硬件验收日期
  （2026-11-10）已更正；
* 被弱化的测试恢复为真实检查 + 回归测试（任意目录启动不漂移状态）。

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



