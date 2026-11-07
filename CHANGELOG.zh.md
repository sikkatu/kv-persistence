# 更新日志

kv-persistence 的所有重要变更，最新的在前。各轮次的变更说明现在放在
这里（长篇技术审计仍在 [docs/final-report.zh.md](docs/final-report.zh.md)）。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

## 第 55 轮（KVP_CACHE_DEVICE 自动检测）

* `KVP_CACHE_DEVICE` 无需手动猜测（用户：“我也不知道，全是 VM + 加密分区”）：
  env.sh 中的共享助手 `_kvp_resolve_cache_device` 从 `live/cache_device`
  （记住）或缓存根的 `findmnt SOURCE` 解析后备设备——支持 VM 磁盘
  （`/dev/vda1`）、LUKS mapper 节点（`/dev/mapper/cryptroot`，经 readlink）
  和 NVMe 分区。test-recovery.sh 与 preflight.sh 使用；显式固定仍优先。
  选择记录在 `live/cache_device`。
* docs/final-report.md 余项清理：LMCache runtime、version-pin 和
  serde end-to-end 行现为 VERIFIED（硬件 2026-11-07）。

## 第 54 轮（硬件验收已记录）

* 重启后恢复的硬件验收已在目标 GPU 主机通过（4x RTX PRO 6000，
  MiMo-V2.6-Pro-MOPD，same-UID，2026-11-07）：
  - 场景 A（重启 vLLM）：cached_tokens=2560，ttft 0s（冷启动 17.53s），
    L1/RAM 命中；
  - 场景 B（两服务均重启，NVMe 保留）：cached_tokens=2560，ttft 0s，
    **L2 读取事件 = 1**（磁盘恢复已证实）；
  - 身份门禁每阶段通过，**0 个硬失败**。
* 机器判定仅因未设置 KVP_CACHE_DEVICE（无法获得独立 NVMe 计数器）而为
  INCONCLUSIVE——在 cache.env.local 中设置后重跑可获机器 PASS。
* README（EN/RU/ZH）、兼容性矩阵（EN/RU/ZH）和 final-report（EN/RU/ZH）
  已更新：ACCEPTED 状态附证据；其余 NEEDS TESTING 项为次要项，如实标注。

## 第 53 轮（cold baseline 等待 API）

* `test-recovery.sh` 的 cold baseline 立即发送第一个 HTTP 请求，若引擎仍在
  加载或已停止则以原始 Python traceback（Connection refused）失败。现在它
  像场景 A/B 一样通过 `api_ready` 等待，连接被拒时打印简短可操作的消息，
  而非 traceback。
* `api_ready` 会说明正在等待什么（不再有静默的 30 分钟循环）。

## 第 52 轮（场景 B：诚实的错误报告）

* `test-recovery.sh` 场景 B：`docker stop` 不再吞掉错误输出
  （`>/dev/null 2>&1` 隐藏了真实原因）。先检查容器状态——已停止/已删除的
  容器不算失败（目标是在引擎停止时干净地重启缓存）。真正的失败会显示
  docker 输出。
* `PA()` 现在显式接收场景标签——旧版即使场景 B 失败也打印 `FAIL(A)`。
  所有调用点已更新。

## 第 51 轮（独立门禁像启动器一样解析 MODEL_DIR）

* 新终端中的独立 `scripts/verify-engine-identity.sh` 报告
  `model_dir=unset` -> num_kv_heads/head_dim UNAVAILABLE -> INCONCLUSIVE，
  而启动器内部的门禁却通过：解析链（live/model_dir > 身份固定值 >
  ~/models）只存在于启动器中。现在共享助手 `_kvp_resolve_model_dir`
  （在 env.sh 中）被两者使用——独立门禁与启动器解析完全一致。
* 修复了新助手中的过期数组 bug（配置目录扫描残留混入模型扫描）。

## 第 50 轮（MODEL_DIR 送达门禁 + 配置中的编解码默认值）

* 自动解析的 MODEL_DIR 从未被导出，因此启动后身份门禁（子进程）看到
  `model_dir=unset`，无法读取 config.json，并拒绝验证 num_kv_heads /
  head_dim（INCONCLUSIVE -> 缓存复用被阻止）。启动器现在在解析后导出它。
* 零配置启动时 `L2 codec: none`：`lmcache-server.env` 仍固定
  `LMCACHE_L2_SERDE=none`，覆盖了第 48 轮的默认值。三个语言文件现均
  一致为 `zstd`；测试锁定以防语言版本分歧。

## 第 49 轮（PATH 自动、监控优先、报告清理）

* 不再需要 `export PATH="$HOME/.venvs/lmcache/bin:$PATH"`（用户要求）：
  `env.sh` 自行解析 lmcache venv 目录（sudo 感知）并为每个脚本前置加入
  PATH。版本预检现通过 venv python 读取主机 lmcache（系统 python3 没有）。
* 监控不是可选项：Quick-Start 第 6 步和 README 快速开始现在先启动
  `start-observability.sh`，再启动 `start-cache.sh`（自动接入，第 47b 轮）。
  「Optional」标注已删除。
* `docs/final-report.{md,ru.md,zh.md}` 删除了所有按轮次的修复章节（用户
  要求：变更在 changelog，早期修复无需记住）——报告只保留已验证状态与
  技术记录，并链接 CHANGELOG。

## 第 48 轮（零配置启动）

* 在新终端中 `bash scripts/run-vllm-with-persist.sh` 无需 export 和标志：
  IMAGE 从配方摘要解析（记入 `live/image`），MODEL_DIR 从
  `live/model_dir`/身份固定值/`~/models` 解析，`KVP_SAME_UID=1` 为默认
  （root 备选：`KVP_SAME_UID=0`），编解码器默认 lossless zstd
  （`USE_LOSSLESS_SERDE=0` 切换到字节精确 raw）。
* env.sh / identity.py / _fold_serde 共用一张编解码器语义表（可执行测试）：
  空 -> zstd 默认，`raw`/`none` -> 显式 raw，开关优先于配置。缓存命名空间
  在 bash 和 Python 中完全一致，零 export。
* 源码注释中的 0.5.3 残留也已清除（文档在第 47 轮完成）。

## 第 47b 轮（可观测性自动检测）

* `start-cache.sh` 自动检测 `127.0.0.1:4320` 上的 OTel 收集器并启用指标——
  无需任何标志（用户要求）。显式关闭：`KVP_NO_OBSERVABILITY=1`；强制开启：
  `USE_OBSERVABILITY=1`；自定义端点：`KVP_OTLP_ENDPOINT=...`。
* README 与 observability README（EN/RU/ZH）已更新：先栈后缓存的流程；
  Grafana 仪表盘描述已刷新。

## 第 47 轮（文档 + 仪表盘诚实性）

* 所有文档现在引用固定的 **LMCache 0.5.6**（README/docs EN/RU/ZH 中
  90 处过时的 `0.5.3` 提法已修正）；对旧上游 bug 的历史引用现在写
  “0.5.6 之前”，不再带过时版本号。
* GPU 空闲显存预检在删除旧引擎**之后**（启动器）和停止两个服务**之后**
  （test-recovery 场景 B）执行——第 46 轮的检查位置过早，会因引擎自身
  合法占用的 VRAM（参考主机每卡 78 GiB）而拒绝。
* Grafana：*LMCache coordinator* 仪表盘已加注释——它需要多服务器协调器
  遥测，在本单服务器仓库中显示“No data”是正常的；*LMCache* 仪表盘
  说明数据仅在缓存服务器启用 `USE_OBSERVABILITY=1` 时出现。
* 创建本 CHANGELOG（EN/RU/ZH）——各轮次说明从现在起放在这里。

## 第 46b/46c 轮（test-recovery 中的编解码门禁 + 说明文档）

* `engine-identity.env` 回退输给 `test-recovery.sh` 中的
  `LMCACHE_L2_SERDE` 环境默认值——优先级修正为
  开关 > 固定值 > engine-identity.env > 环境变量，由可执行测试锁定。
* 说明文档补充了必填标志：wiki Quick-Start 第 7 步和 deployment
  （EN/RU/ZH）展示 `KVP_SAME_UID=1`（模式 A，推荐）与 sudo（模式 B）；
  破损的代码块已修复。

## 第 46 轮（engine-identity.env + 审阅者批处理）

* 启动器将有效编解码器 + 命名空间记录到
  `$KVP_CACHE_ROOT/engine-identity.env`；独立脚本在新终端中读取它，
  无需重复 `USE_LOSSLESS_SERDE`。
* `ipc-test.sh` 中过时的“NOT yet wired”文本替换为真实标志。
* block_size 的 INCONCLUSIVE 行在 `--check-config-only` 下静默。
* test-recovery 场景 B：先停引擎（`docker stop` -> `stop-cache` ->
  `start-cache` -> `docker start`），`api_ready` 在容器死亡时立即中止，
  而非等 20 分钟。
* 新增 GPU 空闲显存预检（`_kvp_gpu_preflight`；正确的检查位置见第 47 轮）。
* deployment 文档（EN/RU/ZH）：重启顺序规则。

## 第 45h–45j 轮（same-UID 模式 + bash 陷阱）

* `KVP_SAME_UID=1` 启动模式：引擎容器以 `$(id -u):$(id -g)` 运行
  （动态 UID，无需 root），缓存置于 `/tmp`。
* 为任意 UID 设置 `HOME`/`USER`/`LOGNAME`（无 passwd 记录时的保险）。
* `PRODUCER_USER` 必须是字面赋值（bash `${var:+NAME=val}` 陷阱），
  由可执行测试锁定。
* 缓存服务器 PID 检查移至 `PERSIST=true` 下。

## 第 45–45g 轮（CUDA IPC 根因 + ipc-test）

* 根因已在硬件上证实：当非 root 缓存服务器打开容器中 root 创建的
  GPU 内存时，`register_kv_caches` 以 `CUDA_ERROR_MAP_FAILED` 失败。
  两种修复：都用 root（`sudo`）或相同 UID（`KVP_SAME_UID=1`）。
* 新增 `scripts/ipc-test.sh` 复现精确的失败步骤（约 1 分钟，不加载模型），
  已接入预检；支持 `PRODUCER_USER` 进行 same-UID 探测。

## 第 42–44 轮（LMCache 版本 + API 漂移 + 诊断）

* LMCache 版本预检：主机和镜像必须一致（0.5.6）。
* 运行时补丁适配 0.5.6 API（`dynamic_store`，文件名中盐的 `@` 后缀
  上游已原生支持）。
* 协议级 RPC ping（`mp_ping.py`），引擎崩溃时自动诊断缓存服务器，
  服务器启动时启用 `PYTHONFAULTHANDLER=1`。

## 第 41 轮及更早

* 真实活性 = TCP 连接（而非仅 PID 存活）。
* 身份门禁拆分为启动前配置检查和启动后容器参数检查。
* 仅无损缓存（zstd/lz4/none，fp8/turboquant 明确拒绝），
  三语文档，可操作的错误信息。