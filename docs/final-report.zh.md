> 🌐 本页其他语言版本：[English](final-report.md) | [Русский](final-report.ru.md) | **中文**

# 最终报告 — kv-persistence

仓库：github.com/sikkatu/kv-persistence（`main` 分支，提交 `HEAD`（见 `git rev-parse HEAD`） —— 已通过 GitHub remote API 验证）。

## 总状态：PARTIAL

实现已完成，离线测试全绿（233 个单元测试 + 19 个离线配置/文档测试 = 252 个离线收集，
250 通过 + 2 跳过；共收集 267 个，含依赖真实 `lmcache` 的集成（10）与 recovery（5）测试；
另有伴生仓库 `sikkatu/recipes` 332 通过 + 5 跳过 / 337 收集——跳过数依赖环境（例如无 torch 的主机跳过 torch 测试）。
硬件验收场景（重启后 A/B 恢复、32K/128K/256K 基准、bytes-per-token）已封装为脚本
（`scripts/test-recovery.sh`、`scripts/benchmark.sh`），待在 GPU 目标主机
上运行。在硬件验证通过前，PARTIAL 是唯一诚实的状态标签。

## 架构与后端选择

存储层使用 LMCache `nixl_store_dynamic` + POSIX（persist_enabled=true；
两个进程都重启后索引惰性重建）；`max_capacity_gb=1500`，LRU 淘汰 + 0.9 软阈值。

## 权重身份 — P0 修复

`scripts/setup-identity.sh` 现在对每个 shard 的**内容**做 SHA-256（而非仅文件名+大小）。
回归测试证实：同名同大小的字节级替换现在会改变指纹（`tests/unit/test_fingerprint_content.py`，
3 条回归全绿）。shard 哈希有缓存加速重复运行。`KVP_FINGERPRINT_MODE=fast` 提供旧模式，
但附带风险警告，仅适合临时实验。

## 引擎身份验证 — 强制门禁

`scripts/verify-engine-identity.sh`（在 `test-recovery.sh` 使用缓存前调用）以
`docker inspect` 的实际引擎参数和 `config.json` 的模型几何核对 identity.env 的固定值。
三种结果：VERIFIED(0) / MISMATCH(4) / **INCONCLUSIVE(5)**。
INCONCLUSIVE = 拒绝复用缓存，绝不猜测。
## LMCache 存储级别的身份隔离（review P1-3）

身份命名空间现在在**真实** LMCache 存储中强制隔离数据，而不仅是本地标签。
已通过固定版 LMCache 0.5.3 源码和实际写入/查找运行验证（集成测试
`test_identity_namespace_isolates_lmcache_storage`，见 docs/benchmarks.zh.md）：

* LMCache 缓存键（`ObjectKey.model_name`）同时构成键和 `nixl_store_dynamic`
  L2 文件名（`<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin`）。
* `CacheIdentity.lmcache_model_name()` 将命名空间嵌入该 `model_name`，
  因此更改**任何**身份字段都会把数据移到不同的键和磁盘上不同的文件。
  按字段证明：更改后的身份查找在 LMCache API 返回 `0/N` 分块，而原始身份
  返回 `N/N`。
* **残余缺口——已在磁盘键层面修复（第 15 轮）**：`cache_salt` 参与键相等性，
  但上游 `nixl_store_dynamic` **不**把它写入文件名（fs/s3/bigtable 适配器会
  包含）。实测泄漏（`store salt=tenantA`、`lookup salt=tenantB` -> 重启后
  `4/4` 分块）已由 `kvpersist/salt_filename_patch.py` 关闭——该补丁把 salt
  追加到 L2 文件名，所有启动器均使用。实测验证：
  `store salt=tenantA`、`lookup salt=tenantB` -> `0/4` 分块
  （`test_cache_salt_isolates_l2_storage` 在泄漏复现时明确失败）。不带补丁
  运行裸 `lmcache server` 保持上游行为：此时请用
  `lmcache_model_name(extra_discriminator=...)` 或独立缓存根目录。

## 恢复测试 — v2 协议（根据审阅意见）

* 日志位置标记；仅统计新事件。
* 重启前先写入验证（L2 必须有真实写入）。
* L2（磁盘）与 L1（RAM）证据区分。
* `cached_tokens == null` → MISSING → INCONCLUSIVE，而非默认当作 0。
* NVMe 物理读取计数器（`/sys/block/<dev>/stat`）作为独立佐证。
* 三态判定：PASS(0) / INCONCLUSIVE(5) / FAIL(1)。

## 已修复的审阅意见（本轮）

| 优先级 | 问题 | 处理 |
|---|---|---|
| P0 | 权重指纹不含 shard 内容 | SHA-256 逐 shard 内容哈希 + 回归测试 |
| P1 | 预算文档 750/1TB 与代码 1.5TB 矛盾 | 三语 wiki 同步为 1500；边界 = 设备可用空间 |
| P1 | identity.env vs identity.env.local 混淆 | 全部文档指向 .local（setup-identity 写入处） |
| P1 | 本地指纹 vs HF SHA 未区分 | troubleshooting/deployment 明确两种格式与适用场景 |
| Quick-Start 步骤 | 缺 API_KEY/recipe/MODEL_DIR 前置条件 | 步骤 7 列明三项必需前置 |
| Quick-Start 步骤 | 手动复制 digest 要求 | 已移除；setup-identity 自动写入 |
| Quick-Start 步骤 | Python 3.12/venv 在 Debian/Ubuntu (tested on Debian 12) | 明确安装命令与 `python3.12 -m venv` |
| README | 测试数 29→49、"not code"、"as-is" | 已更正 |

## 第 14 轮审阅修复（已在本机验证）

* **严重**：`run-vllm-with-persist.sh` 未传入 `--block-size`，导致启动后身份门禁
  永远无法核验 `block_size`（INCONCLUSIVE，exit 5），启动器随即
  `docker rm -f` 刚创建的容器。现在向 vLLM 传入
  `--block-size "${KVP_BLOCK_SIZE:-16}"`（`test-recovery.sh` 的门禁也因此通过）。
* `l2_serde` 现已真正进入身份命名空间：`model-name` 按 bash 加载器的相同顺序
  读取 generic 配置层（`LMCACHE_L2_SERDE` 所在处），因此 `none` 与 `fp8`
  产生**不同**的名字。进程环境变量优先于文件（与 `scripts/env.sh` 一致）。
* `purge --namespace` 仅按**精确**匹配命名空间。真实命名空间形如
  `--models--MyModel--<hash>`（以短横线开头），因此文档给出 `--namespace=NAME`
  形式（或新的位置参数形式）；`--namespace NAME` 会被 argparse 解析为未知选项。
  无匹配项时以退出码 1 和明确消息结束。带 `_` 的文件名用右锚定正则解析
  （`my_model` 不再被截成 `my`）。
* `retention.py`：命中权重变得有意义。公式为
  `(hits + 1) × tokens × 新鲜度 ÷ 字节`——在每 token 字节数恒定时，对 hits
  相同的条目长度中性（旧的 `log1p` 会优先淘汰长前缀；更早的
  `3*hits + 1.5*tokens` 使 hits 仅占约 1%）。若要显式保护长条目——提高
  `min_keep_tokens` 或给 `tokens` 加一个小的正指数。
* 两个配置加载器优先级逐字节一致：Python 加载器按 bash seeder 的相同顺序
  应用文件（cache.env -> lmcache-server.env -> identity.env -> *.local）。
* systemd：`kvp-lmcache-server` 权限 100755 含安装说明；按需渲染 L2 规格
  并读取 `cache.env`/`.local` 中的 `KVP_CACHE_ROOT` 与预算（与
  `start-cache.sh` 相同的优先级——`cache.env.local` 中的自定义缓存根目录
  会被尊重，不会在系统盘上悄悄放 1.5 TB）；`StateDirectory=kv-persistence`
  处理默认路径的权限；自定义 `KVP_CACHE_ROOT` 时执行一次
  `sudo install -d -o kvp -g kvp -m 0700 <dir>`；`KVP_REPO_ROOT` 默认为
  `/home/kvp/kv-persistence`（README 克隆到 `~/kv-persistence`；可在
  `/etc/kv-persistence.env` 覆盖）。
* 文档同步（EN/RU/ZH）：`eviction-and-deduplication.*` 公式更新为线性形式；
  `storage-management.*`、`security.*` 与 `final-report.*` 描述真实的 L2 布局
  （扁平 `<safe_model_name>_<rank8>_<group>_<hash64>.bin` 文件）而非不存在的
  `kvp-<hash>` 目录；`deployment.*` 新增含权限与 `--namespace=NAME` 形式的
  systemd 章节。

## 第 15 轮审阅修复（已在本机验证）

* **cache_salt 隔离已在磁盘键层面修复**：上游 0.5.3 不把 salt 写入
  `nixl_store_dynamic` 的 L2 文件名（实测泄漏：salt=tenantA 写入 → salt=tenantB
  查找返回 4/4 分块）。新增 `kvpersist/salt_filename_patch.py` 把 salt 追加到
  文件名；所有启动器（`scripts/start-cache.sh` host+docker、
  `systemd/kvp-lmcache-server`）都通过它启动服务器。实测验证：
  tenantA → tenantB = 0/4（`test_cache_salt_isolates_l2_storage`——该测试现在
  **要求**隔离成立，泄漏复现即明确失败）。空 salt 保持上游命名（向后兼容）；
  未打补丁的服务器带 salt 写入的数据是安全未命中，绝不会错误读取。
* **README 兼容性声明已收窄**（EN/RU/ZH）：「适用于任何模型」->
  「设计用于任何 vLLM 模型的复用；经硬件确认的仅有 MiMo-V2.6-Pro +
  4× RTX PRO 6000（sikkatu/recipes）；其他模型在通过验收测试前属实验性兼容」。
* **README.ru 安全表述已修正**：「Никаких неаутентифицированных эндпоинтов
  управления」不正确——管理端点（HTTP + ZMQ）仅限 loopback，但这不是身份
  验证；同一主机的其他本地用户仍可访问。三个 README 语言版本均已改写
  （与 docs/security.* 一致）。
* **KV dtype BF16 全面确认**（按审阅指示）：启动器默认值、`identity.env`
  固定值与身份门禁均使用 bfloat16；声称「目标是 fp16」的注释已更正
  （identity.env.* 与 run-vllm-with-persist.sh 的 EN/RU/ZH；compatibility.* 表）。
  KV dtype 仍是缓存身份的一部分——dtype 变更即未命中，绝不会按错误格式读取。

## 已知限制（同前）
1. 硬件验收（A/B 测试）未在本会话执行——目标主机不可达；脚本与协议完全就绪。
2. LMCache 0.5.3 与生产镜像 PyTorch/CUDA 组合待硬件最终验证。
3. 去重文档已完整记录；实际效果需在目标配置上度量。

## 第 13 轮审阅修复（已在本机验证）

* `setup-identity.sh` 不再写入带引号的值（其自带校验曾报 `got '"76ce..."'`）；
  读取时同样剥离引号。
* `run-vllm-with-persist.sh`：身份门禁在启动前作为纯配置检查
  （`--check-config-only`）执行，并在 `docker run` 之后对**新**容器参数再次执行
  （旧顺序检查的是尚未创建或参数过期的容器）。`KVP_PERSIST_ENABLED=0` 会真正
  跳过两个门禁。
* 真实 vLLM 路径上的按身份隔离已生效：模型挂载到
  `/models/<lmcache_model_name>`（名称编码 namespace），因此
  `model_config.model`（LMCache 据此构建 ObjectKey 与 L2 文件名）随身份不同而
  不同（此前统一为 `--model /model`）。
* `kvpersist.cli` 能看到真实 L2 布局（适配器写入的扁平 `*.bin` 文件）：
  `status` 统计其字节，`purge --all` 与 `emergency-evict` 真正删除它们。
  预算默认 1500 GB（原 750）。`KVP_CACHE_ROOT`/`KVP_CACHE_BUDGET_GB` 会回退到
  仓库配置文件，因此 `emergency-evict` 可从 cron 运行。配额被如实记录为仅报告用。
* systemd 单元：`lmcache-server.service`（普通，`User=kvp`）与
  `lmcache-server@.service`（模板，`User=%i` —— `%i` 唯一合法形式），以及内联
  渲染后适配器 JSON 的 wrapper `systemd/kvp-lmcache-server`
  （`--l2-adapter` 接受 JSON 字符串而非路径）。
* `test-recovery.sh`：硬 FAIL 不再被 INCONCLUSIVE 覆盖（`V`/`VI` 分开计数，
  FAIL 优先）；冷前缀携带每次运行的 nonce，使重复运行真正是冷运行。
* Docker 模式：`--help` 探测在容器内进行，服务器在容器内监听 `0.0.0.0`
  （宿主机端口仍为 loopback），新增 `--gpus all --ipc host`，日志行不再因
  未设置变量而崩溃。
* `retention.py`：前缀长度项改为线性（`log1p` 版本会优先淘汰长的高复用前缀）；
  `min_keep_tokens` 被记录为唯一直接保护。`prompt_probe.py` 序列化
  `tool_calls`/`tool_call_id`。
* `preflight.sh`：`lsblk -p` 输出不再产生 `/dev//dev/...` 形式的设备名。
  文件 `.commitmsg11` 已从仓库中移除。
## L2 无损编解码与可观测性栈（本轮任务）

本轮任务要求两项补充；两者均在**不 fork LMCache**、不改动生产配方的前提下实现。

### 1. L2 仅无损存储

上游 LMCache 0.5.3 仅注册有损（fp8、turboquant）与加密（aesgcm）serde ——
上游不存在无损压缩 serde。本仓库通过 `implementation/kvpersist/compress_serde.py`
在运行时向 LMCache 自身注册表注册 `kvp-zstd` / `kvp-lz4` / `kvp-raw` serde
类型（与 salt 文件名补丁相同的启动器模式，现已统一到
`implementation/kvpersist/launch.py`）。

* 默认 `LMCACHE_L2_SERDE=none` —— 逐字节精确的原始块，与未压缩的 upstream
  路径完全一致。不做任何变换。
* `LMCACHE_L2_SERDE=zstd|lz4`（或操作开关 `USE_LOSSLESS_SERDE`）—— 无损压缩。
  无法压缩变小的块按原样存储，因此块永远不会膨胀超过一个 4 KiB 页
  （28 字节帧头 + 填充到页边界）。常被引用的 1.1–1.4x 缩减是来自 LMCache
  生态的**规划估算**，并非本仓库的实测结果：这里尚未运行压缩率基准测试。
* 有损编解码（fp8 / turboquant / lbq / cachegen / kivi）在启动时被明确
  **拒绝** —— 无损是契约，而不是可以漂移的默认值。
* 编解码被折叠进缓存身份命名空间（`l2_serde`），shell
  （`scripts/env.sh _kvp_resolve_l2_serde`）与 Python 加载器
  （`kvpersist.identity`）均如此；单元测试断言两者一致
  （`tests/unit/test_lossless_serde_switch.py`）。切换编解码是安全的缓存
  miss，绝不会错误命中。
* 帧格式 `KVCZ` v2 携带未压缩负载的 CRC32：被截断的 DMA 读取、位翻转或
  来自其他编解码的帧会在反序列化时被检测并报告为缓存 MISS。
* 负载之后的填充被显式清零 —— 回收的 L1 slab 块绝不能把其他租户的字节
  泄漏进我们的缓存文件（`tests/unit/test_compress_serde.py::test_padding_is_zeroed`）。
* Fail-fast：服务器启动前，`require_codec()` 只验证**所选**编解码的绑定
  （host venv 探测，或 docker 模式下**容器内**探测）。`LMCACHE_L2_SERDE=none`
  的缓存根本不需要任何压缩包 —— lz4 与 zstandard 仅在其编解码被实际选用时
  才必需。缺失绑定会中止启动，而不是达到 READY 后在 serde 线程池中失败。
* L2 文件大小补丁仅在真正构建 kvp-* serde 时安装；`LMCACHE_L2_SERDE=none`
  时适配器类完全不动（逐字节 upstream load/store），因此 raw 路径上短的
  DMA 读取绝不会被我们的包装器掩盖。

**如实状态：** serde 帧逻辑已通过单元测试（往返、成帧、坏帧拒绝、不可压缩
数据透传），且在未安装 torch/lz4/zstandard 的环境中运行。经 LMCache 的
`SerdeL2AdapterWrapper` 与 `nixl_store_dynamic` DMA 写入的端到端路径
**需要在目标 GPU 主机上测试** —— 与其余硬件验收套件状态相同。
两处运行时补丁（`_install_size_patch`）使磁盘文件大小跟随序列化后的帧，
而非完整 L1 分配；这些已被单元级逻辑覆盖，但尚未在硬件上验证。

### 2. Prometheus + Grafana 监控

`observability/` 内联了 LMCache 官方示例栈
（`LMCache/LMCache` `examples/observability`，Apache-2.0）：OTel Collector +
Tempo + Prometheus + Grafana，并预置官方 LMCache 与 Coordinator 仪表盘
（JSON 逐字复制）。

```bash
bash scripts/start-observability.sh     # 一条命令，Grafana 位于 :3000
```

遥测为可选项且默认关闭（`KVP_OTLP_ENDPOINT` / `USE_OBSERVABILITY=1`）；
缓存服务不依赖该栈也能完整工作。
**状态：compose 文件与仪表盘为逐字官方资源，脚本已通过语法检查；本次会话
未在目标主机上启动实际栈**（GPU 主机 / Docker 守护进程不可达）。

## 第 17 轮评审修正

**关键（破坏启动/验收）：**
* **`--kv-cache-dtype bf16` 无效**：固定版本 vLLM 8e2beaa3 的 `CacheDType`
  只接受 `float16`/`bfloat16`，而非 `fp16`/`bf16` —— 启动会在参数解析处崩溃。
  已在各处修正（启动器、`identity.env` ×3、preflight 现在会对 `bf16`/`fp16`
  发出警告，测试与文档使用 `bfloat16`）。身份命名空间随之改变 —— 安全的缓存
  未命中。
* **`test-recovery.sh` 不可能 PASS**：启动器未传 `--enable-prompt-tokens-details`，
  因此 `usage.prompt_tokens_details` 恒为 `None`，`cached_of` 读到 0/MISSING。
  该标志现在对任何模型都传递。
* **日志正则与 LMCache 0.5.3 真实日志不符**：真实证据行是
  `L2 store completed: adapter N, N succeeded`、
  `L2 prefetch load completed: request N, N loaded`（均 DEBUG 级别）和
  `Prefetch request completed (L1+L2)`（INFO）。旧模式只能匹配错误行。新正则
  匹配真实字符串，并新增 `LMCACHE_LOG_LEVEL`（默认 `DEBUG`）—— 否则 DEBUG 行
  根本进不了日志。

**严重（文档撒谎 / 保护弱于声称）：**
* **重启后硬上限失效** —— 索引惰性重建、磁盘不被扫描、用量从零开始。已在
  README 与 storage-management 中明确标注（NEEDS TESTING + 必需的硬件测试）。
* **systemd 路径未设 0700** —— `StateDirectory=` 创建的是 0755。包装脚本现在
  强制 `chmod 700`，否则拒绝启动。
* **verify-engine-identity 被高估**：比较的是容器启动参数与 `config.json`，
  不是活的引擎；环境被忽略；注释承诺的 connector 检查未实现 —— 现已实现，
  消息也如实描述检查范围。
* **`"model":"mimo-pro"` 硬编码** —— 替换为 `SERVED_MODEL_NAME`。

**次要：** `identity.env` 写入目标矛盾已修正（追踪文件从不编辑，值写入
`identity.env.local`）；OTLP 标志现在由 systemd 包装脚本转发；
`KVP_CACHE_BYPASS` 标记为 DEPRECATED；`KVP_PERSIST_ENABLED` 语义已澄清；
`emergency-evict` 如实描述为 **FIFO（按 mtime）**，而非 LRU；
`budget.evict_candidates` 竞态修复（迭代移入锁内）；`budget-report.sh` 的
字面 `\n` 修正；`retention.py` 的 `now or time.time()`（改为 `is None`）；
`identity.env` 中 shard 指纹注释已与真实实现同步。

**措辞：** 「lmcache 不依赖 nixl」已修正（上游存在 `lmcache[nixl]` extra）；
benchmarks.md 的验收标准改为脚本实际匹配的字符串；未经证实的
「urllib 缓冲导致 +0–500 ms TTFT」声明已删除（SSE 迭代是逐行到达的）。

## 第 18 轮评审修正

**关键（误报 FAIL / 虚假安全感）：**
* **场景 A 在正常系统上会误报 FAIL**：`test-recovery.sh` 在场景 A（仅重启 vLLM）
  没有 L2 读取事件时硬性判定 FAIL。但缓存服务及其 L1（主机内存）在该重启中存活，
  健康系统会从 L1 提供请求并记录 `(N L1, 0 L2)` —— 即文档中定义的「memory hit」。
  现在 A 接受 memory hit（仅凭 `cached_tokens>0` 即 PASS），磁盘信号仅供参考，
  只有场景 B（完整重启、L1 为空）才必须出现 L2 读取事件。最终裁决中的
  「有复用但无 L2 证据」检查也从 A+B 移至仅 B。要求 `(N L1, N L2)`（两个计数器
  均非零）的正则保留为 B 的佐证信号，但不再是唯一关卡。
* **tmp_guard 在 docker 模式下完全失效（P1）**：`start-cache.sh` 的 docker 模式在
  **不带** `--pid=host` 的容器中启动服务，因此标记中的 PID 属于容器的 PID 命名空间
  —— 主机 CLI 拿它去对照主机的 `/proc`，找不到该进程，于是把**正在写入**的存储
  判定为已死；实际上只剩 15 分钟的 mtime 窗口在起作用。已在机制层面修复：
  `register_write` 现在把写入进程的 PID 命名空间 id（`/proc/self/ns/pid`）写入
  标记，`writer_alive` 在标记属于外来命名空间时回退到标记自身的 HEARTBEAT
  （mtime 新鲜（≤ `HEARTBEAT_ALIVE_SEC`）= 存活，过旧 = 崩溃残留）。同一命名空间
  的标记仍按原逻辑精确检查 /proc(pid, start-time)。旧的两行标记（空 ns）保持
  原行为。测试：`test_writer_alive_falls_back_to_heartbeat_in_foreign_pid_ns`、
  `test_marker_records_writer_pid_namespace`。

**严重（仪表盘不实 / 场景竞态）：**
* **Grafana「Cache Hit Rate」（及另外 3 个面板）在 0.5.3 上为空**：内置仪表盘是
  从比固定的 0.5.3 更新的 LMCache 复制的。通过比对 0.5.3 源码中每个
  `create_counter`/`register_gauge`（`lmcache/v1/mp_observability/`）确认：28 个
  面板中有 **4 个**引用了 0.5.3 从不输出的指标 ——
  `lookup_prompt_tokens_total` / `lookup_request_hit_tokens_total`（Cache Hit
  Rate、Lookup & Engine）、`num_submitted/finished_stores|retrieves`
  （Inflight GPU Copies）、`transfer_phase_*`（MP Transfer DMA）。这些面板已改用
  **真实的 0.5.3 指标**（lookup_requested/hit、l1_read/l1_write、
  l0_l1_store_throughput）。修复过程中的**重要更正**：`l1_memory_usage_bytes`、
  `l2_usage_bytes`、`num_inflight_l2_*`、`active_prefetch_jobs` 以及
  `event_bus_*` gauge 在 0.5.3 中**是存在的**（通过 `register_gauge` 在
  `l1_manager.py`/`storage_manager.py`/`lookup.py` 中注册）—— 第一版补丁错误地
  连它们一起替换了；该过度替换已回退。README 现在明确说明「官方」仪表盘是针对
  更新版本的 LMCache 编写的，列出被替换的 4 个面板，并说明 **Coordinator** 仪表盘
  合理地保持为空（本仓库不运行 coordinator；其 48 个指标在 0.5.3 中没有对应物）。
* **L2_bytes 把注册表标记算进了总量**：`.kvp-active-writes/` 中的标记是临时簿记，
  不是缓存数据 —— 把它们算进去可能导致误报「L2 shrank」。现在已从
  `L2_bytes`/`find` 中排除。
* **异步 L2 写入与字节测量竞态**：`ask cold` 之后立即 `L2_bytes` 没有等待，但写入
  是异步完成的 —— 可能误报「nothing was stored」。`wait_l2_quiet()` 现在会反复
  采样 `L2_bytes` 直到其稳定后，再进行 B0/B_WARM 和 B_BEFORE/B_AFTER 比较。

**次要：**
* **preflight 对 `bf16`/`fp16` 只警告**：CacheDType 拼写错误会让引擎拒绝启动，
  因此 preflight 现在直接 `die`，而不是警告后继续注定失败的 `docker run`。
* **注释中的拼写错误「BLOAT16»** 已修正（BFLOAT16）。
* **`ask()` 的 heredoc 变成了未加引号**（`<<PYEOF`）：Python 块中任何 `$` 或反引号
  会被 shell 展开。已恢复 `<<'PYEOF'`（加引号），且 `SERVED_MODEL_NAME` 现在
  EXPORT —— Python 块通过 os.environ 读取它。
* **`LMCACHE_LOG_LEVEL` 默认值与建议不一致**：配置文件默认 DEBUG，而注释建议生产
  用 INFO。现在默认值被明确记录为有意为之（验收证据行需要 DEBUG），该值由
  `scripts/env.sh`、`systemd/kvp-lmcache-server`（经 `repo_cfg`）以及
  docker 容器（`-e LMCACHE_LOG_LEVEL=…`）一致读取 —— 三种启动模式统一。
* **DEBUG 日志的 token 泄漏检查（评审要求）**：已对固定的 0.5.3 中每个
  `logger.debug` 调用做静态审计 —— DEBUG 日志只包含计数、大小、session/request
  id 和块键哈希，从不包含 token id 或提示词文本。唯一涉及原始键材料的路径是
  可选的 `--lookup-hash-log-dir` JSONL 文件记录器（默认关闭，需操作员显式设置）。
  已在 docs/security.md（EN）和配置注释（EN/RU/ZH）中记录。

## 第 19 轮评审修正（与 sikkatu/recipes 的跨仓库一致性）

**P1 —— 缓存身份命名空间在配方路径下不工作**：配方启动器把 `MODEL_DIR` 挂载到
`/model`，LMCache 对每个部署都收到相同的 `model_name`（`model_config.model` =
`--model` 值；已对照 0.5.3 的 `lmcache_mp_connector.py:680` 验证）。不同版本、
KV dtype 和聊天模板共享一个命名空间 —— README 的说法只对 kv-persistence 自己的
启动器成立（第 13 轮已修复）。现已在**两个**配方启动器中修复（`run-tp4-uva.sh`、
`run-tp4-hotsplit.sh`，相同的块）：`KV_PERSIST=1` 时模型挂载到
`/models/<缓存身份命名空间>`（取自 `kvpersist.cli model-name`，若找到
kv-persistence 克隆；否则用 KV 兼容性字段的确定性哈希）。`KV_PERSIST=1` 现在
**必须**设置 `MODEL_DIR`（远程下载无法按版本隔离 —— 启动器拒绝）。配方中的契约
测试固定了该机制且不影响普通启动。

**P1 —— wiki 的「Verify the engine geometry」步骤无法通过**：两处已修复 ——
`KV_CACHE_DTYPE` 默认从 `auto` 改为 `bfloat16`（与 kv-persistence 启动器一致），
`--block-size 16` 现由 `KV_PERSIST=1` 分支固定。独立地，`verify-engine-identity.sh`
现在**解析**值而非拒绝：`auto` 解析为 `config.json` 中的 dtype（引擎实际计算的
有效 dtype），缺失的 `--block-size` 通过 `config.json` 或固定 vLLM 默认值（16）
解析，而不是报告 MISSING/INCONCLUSIVE。

**P1 —— 配方路径缺少 `--enable-prompt-tokens-details`**：没有它 `cached_tokens`
不会上报，`test-recovery.sh` 在配方启动的服务器上永远无法 PASS。
`KV_PERSIST=1` 分支现在传递该标志。

**P2 —— 「逐项镜像参数」不准确**：`MAX_NUM_SEQS` 现在两条路径都是 2，dtype 两条
路径都是 `bfloat16`；`run-vllm-with-persist.sh` 的头部注释如实说明「基线 + 四个
已记录的补充」。wiki *配方集成* 中的表格列出所有差异：值差异是**安全的缓存未命中**，
绝不会错误读取。

**CI**：kv-persistence 原本没有 workflow（评审者无法确认测试运行）。现已创建
`.github/workflows/check.yml`：离线 unit+配置测试（无需 lmcache/GPU）、所有启动器
的 bash 语法检查、所有模块的 py_compile。

**测试计数**：此前的数字已过时。实际：233 个单元测试（231 通过 + 2 跳过），19 个离线配置/文档测试，
252 个离线收集（250 通过 + 2 跳过），共收集 267 个（另有伴生仓库 `sikkatu/recipes` 332 通过 + 5 跳过 / 337 收集；
跳过数依赖环境：torch/lz4/zstd）。已在
final-report（EN/RU/ZH）与 Home（EN/RU/ZH）中修正。

**端口命名**：三个变量指向同一端点 —— 已在 *配方集成*（EN/RU/ZH）与配方 README
**TP 命名跨仓库一致性、且 TP != 4 未经测试（评审备注）**：`TP_SIZE`（配方）
**共享的有效标识解析器（rev. 23）**：引擎的有效配置（auto -> 模型 dtype、
**引擎参数的唯一事实来源（rev. 24）**：引擎以解析后的有效值启动——
**Smoke 请求跟随引擎（rev. 25）**：启动器将 served-model-name 显式传给
**跨仓库一致性（rev. 26）**：(1) cache.env(.local) 中的 KVP_LMCACHE_* /
**编解码器检查跟随服务器（rev. 27）**：l2_serde 检查现在从
**分层配置解析（rev. 28）**：(1) cache.env 中的 KVP_* 别名现已注释——
**解析器一致性 + 端到端门禁测试（rev. 30）**：bash 解析器 env.sh 现在
**测试安全性 + 严格 env 格式（rev. 31）**：测试 fixture 现在会备份并恢复
**仅 ASCII 名称 + 全字典一致性（rev. 32）**：python `str.isidentifier()`
**HOME 展开一致性（rev. 33）**：`identity._read_env_config()` 完全不展开
**recipe 启动器强制门禁 + 钉住兼容版本（rev. 34）**：recipe 启动器
**启动后完整 identity 门禁 + kv-persistence 为必需依赖（rev. 35）**：
**安装修复：openai 依赖 + API 密钥自动生成（rev. 36）**：`lmcache server`
**评审批次，第 37 轮**（每个问题都先对照代码核实）：
**第 38 轮补充**（第 37 轮修复需要完善）：
**第 39 轮**：`ensure_cache_root`（systemd 包装器）不应 chmod 700 自定义
**第 40 轮（真实安装中发现的问题）**：
**第 41 轮（docker run 之前的预检）**：安装运行损失了 24 分钟——identity
**第 42 轮（LMCache 版本对齐）**：第二次安装运行再次在
**第 43 轮（LMCache 0.5.6 API 漂移）**：版本对齐后缓存服务器启动即崩溃——
**第 44 轮（RPC ping + 服务器自动诊断）**：版本对齐和 API 修复后引擎仍在
**第 45 轮（CUDA IPC 根本原因）**：服务器日志终于揭示了真正的故障——
`Error handling operation register_kv_cache` ->
`torch.AcceleratorError: CUDA error: mapping of buffer object failed`
/ `cuIpcOpenEventHandle` 返回 `CUDA_ERROR_MAP_FAILED`。缓存服务器（主机，
用户 `de`）无法打开 vLLM 容器（root）创建的 CUDA IPC 内存。处理函数无响应
死亡——vLLM 等了 300 秒。不是版本问题，也不是 RPC 协议（ping 通过）。修复：
* 新增 `scripts/ipc-test.sh`——约 1 分钟内、无需加载模型即可复现恰好失败的
  步骤：容器内 producer 创建 GPU 张量，consumer（host / host-root /
  container）通过 IPC 打开。接入预检：损坏的边界在 docker run 之前被拒绝
  ——附修复命令（在容器中或以 root 运行缓存服务器）。可通过
  `KVP_SKIP_IPC_TEST=1` 跳过（不推荐）。
* 诊断现在优先显示 `Error handling operation` 异常（此前：30 行 eviction
  噪声掩盖了它），并从尾部过滤 `skipping eviction`。
* 等待循环在服务器端新错误时立即失败（不再浪费 300 秒注册超时）。
* `ipc-test.sh` 现在自行从 recipe 解析 IMAGE——命令就是
  `bash scripts/ipc-test.sh`，无需填写占位符。
* Docker 模式：`start-cache.sh` 警告缓存文件属主为 root（purge/budget
  需 sudo）；`purge` 在 PermissionError 时打印原因和修复方法，而非仅错误。
7 个测试（233 unit / 252 offline / 267 共计）。

`register_kv_caches` 死掉（300 秒超时）——而 TCP 预检通过了。套接字开着，
但 RPC 通道已死：TCP 可达性不能证明 lmcache 消息协议正常。修复：
* 新增 `kvpersist.mp_ping`——协议级探针，构建与 vLLM 连接器相同的
  RequestClient 并发送 `ping`（无需注册）。接入启动器预检：接受 TCP
  却不回应 RPC 的服务器在 docker run 之前被拒绝——错误信息中给出确切的
  修复命令；
* 当引擎容器退出时，`run-vllm-with-persist.sh` 现在自动打印缓存服务器
  诊断（进程存活、日志新鲜度、最后 30 行日志、[kvp] 状态）——故障后不再
  需要手动取证；
* `start-cache.sh` 以 PYTHONFAULTHANDLER=1 启动服务器（两个分支）：
  挂起的线程在 SIGABRT 时倾倒所有线程堆栈——挂起可通过堆栈识别，而不是
  靠沉默。
5 个测试（226 unit / 245 offline / 260 共计）。

`AttributeError: DynamicNixlStorageAgent has no attribute
'dynamic_store_file'`。LMCache 0.5.6 重构了 L2 适配器：代理移至
`l2_adapters.nixl_store_agents`，存储方法改名为 `dynamic_store(mem_indices,
key)`。更关键的是，0.5.6 上游修复了盐泄漏——`_object_key_to_filename`
现在追加 `@salt`。两个补丁现在都是版本感知的：
* `salt_filename_patch.install()` 在 0.5.6+ 返回 `upstream`（无需补丁——
  上游已隔离），在 0.5.3 返回 `patched`。旧的 `_` 后缀命名可通过
  rendered 适配器中的 `legacy_salt_suffix` 启用（仅用于迁移）；
* `tmp_guard.install()` 包装具体类 `FileDynamicNixlStorageAgent` 上的
  `dynamic_store`（不是 ABC 的抽象方法），并保留 0.5.3 的
  `dynamic_store_file` 回退；
* `launch.py` 打印「cache_salt included in L2 file names by upstream
  LMCache (0.5.6+ native '@'-suffix) -- no patch needed」。
9 个测试（221 unit / 240 offline / 255 共计）。

`register_kv_caches` 失败——根本原因是版本分裂：固定的 vLLM 镜像自带
lmcache **0.5.6**（容器内的客户端），而主机 venv 提供 lmcache **0.5.3**
（服务器）。混用小版本会静默破坏 MP 协议。修复：
* `requirements-lmcache.txt` 现在固定 `lmcache==0.5.6`——镜像是唯一
  真相来源（其连接器即客户端）；
* 启动器预检提取两侧版本（主机 venv + 镜像），不匹配则拒绝启动——在
  docker run 之前；
* 当渲染适配器使用 `cache_salt` 时，`salt_filename_patch` 失败现在是
  致命错误（此前是静默警告——lmcache 升级重命名被打补丁的函数时，跨租户
  隔离会悄悄失效）；
* `docs/compatibility.md`（EN/RU/ZH）：LMCache 行更新为 0.5.6。
2 个测试（212 unit / 231 offline / 246 共计）。

门禁通过、vLLM 开始加载，直到 `register_kv_caches`（300 秒超时）才因
"LMCache server did not respond" 失败。`run-vllm-with-persist.sh` 现在
在 `docker run` 之前检查：(1) L2 编解码器与运行中服务器的
`l2-adapter.rendered.json` 一致（不匹配立即中止并给出确切修复方法），
(2) 缓存服务器在请求端口上确实能应答——TCP 探测取代了单纯的 PID 检查
（活 PID 不等于可工作的服务器，安装运行正是如此）。3 个测试
（210 unit / 229 offline / 244 共计）。

* `setup-identity.sh` 现在从模型的 config.json 填充 KV 几何
  （`KVP_NUM_KV_HEADS` / `KVP_HEAD_DIM`）。模板硬编码 head_dim=128，
  而 MiMo-V2.6-Pro 实际为 192——全新 setup 产生的 PIN 随后被门禁对真实
  引擎判为 MISMATCH。模板注释现在说明这些值只是示例、会被
  setup-identity.sh 覆写。
* 门禁的 MISMATCH 输出现在带有可操作提示：`l2_serde` 给出编解码开关
  修复（服务器与启动用同一 `USE_LOSSLESS_SERDE`，或不带压缩重启）；
  `head_dim`/`num_kv_heads` 指向重跑 `setup-identity.sh`。（门禁本身
  工作正常——评审运行中的两个不匹配是真实的失步，但消息让操作员无从
  下手。）
* `start-observability.sh`：Grafana 等待每 2 秒打印相同行（看似卡死），
  且冷启动 60 秒太短——现在最多每 10 秒打印变化的进度（容器状态），
  等待上限 180 秒。
* 测试：3 个新增（207 unit / 226 offline / 241 共计）。

`KVP_CACHE_ROOT` 的任意父目录——当 `KVP_CACHE_ROOT=<共享>/kvc` 时，父目录
（如 `/data`，755）变成 700，主机上其他用户全部被挡在外面（评审者在旧代码
路径上实测确认）。父目录收紧现在仅限于 StateDirectory 默认路径
`/var/lib/kv-persistence`；自定义缓存根的父目录不属于我们的管辖范围。
缓存根目录本身保持 0700 安全契约（docs/security.md）。1 个回归测试
（204 unit / 223 offline / 238 共计）。

* systemd 包装器：`ensure_cache_root` 现在在写入渲染适配器之前运行——首次
  启动时缓存目录尚不存在，见证文件被静默跳过（门禁 exit 5 直到服务重启）。
* `LMCACHE_HTTP_HOST=0.0.0.0` 现在在 start-cache.sh 和 systemd 包装器中都被
  拒绝（此前只检查 LMCACHE_HOST 的回环，HTTP 管理前端可以绑定任意地址）。
* start-cache.sh 的水位线：渲染的 JSON、摘要行和服务器标志现在共享同一个
  解析变量（此前 JSON 0.9 vs 服务器 0.8）。
* start-cache.sh："已在运行"守卫现在在适配器渲染之前执行——被拒绝的第二次
  启动曾经用新运行的值覆盖见证文件（评审者测得 kvp-zstd/1 覆盖在存活的
  zstd=none/1500 服务器上）。
* 门禁：没有 MODEL_DIR 的 `--kv-cache-dtype auto` 现在返回 INCONCLUSIVE
  （exit 5），而非误报 MISMATCH（exit 4）——model_cfg 返回 "UNAVAILABLE"，
  但代码只检查了 "MISSING"。
* select_victims（r37 修复曾引入回归）：非共享条目优先排名（它们释放真实
  字节），按名义字节停止——既无批量逐出（r37a），也无释放不足（r37b）。
* requirements-lmcache.txt：openai 固定块被复制了两次——保留一份。
* 测试：6 个新回归测试（203 unit / 222 offline / 237 共计）。

* HIGH —— systemd 包装器（`kvp-lmcache-server`）从不写
  `l2-adapter.rendered.json`，因此强制的启动后门禁对 systemd 启动的
  缓存总是返回 exit 5（INCONCLUSIVE），recipe 启动器失败；包装器现在
  与 `start-cache.sh` 完全一致地写入渲染后的适配器。
* HIGH —— 手动运行门禁时用合成默认值替换 identity 的 PIN（`KVP_TP_SIZE=2`
  变成 4 -> 误报 MISMATCH）；effective-identity 导出现在只在操作员真正
  设置了源名称时才折叠值。按 `identity.env.local` 的 PIN 手动运行现在通过
  （已用回归测试锁定）。
* HIGH —— `_proc_start_time` 返回 "unknown" 时 `tmp_guard.register_write`
  抛出 TypeError（写入从未注册，purge 可能删除其 .tmp 文件）。
* `salt_filename_patch.apply()` 在 upstream 重命名被补丁的函数时返回 True
  （静默丢失隔离）——现在先验证目标存在（评审者建议优先检查；真实的 lmcache
  仍然无法获得，针对存根测试）。
* `start-cache.sh` 拒绝在存活的服务器上启动（此前会覆盖 PID 文件并使其
  变成孤儿）；`stop-cache.sh` 在终止前校验 `/proc/<pid>/cmdline`（过期 PID
  绝不误杀无关进程）。
* systemd 包装器对齐：LMCACHE_HOST 的回环检查（此前接受 0.0.0.0）、HTTP/OTLP
  从仓库配置读取、$HOME 展开、适配器 JSON 中的水位线与服务器标志现在同源
  （repo_cfg）；模板单元的 EnvironmentFile 顺序已修复。
* `purge --tmp`/`--all` 不再删除名称仅含 `.tmp.` 的有效条目——只有适配器
  崩溃残留模式 `<final>.bin.tmp.<uuid>` 是目标（安全 MISS，不丢缓存）。
* `retention.select_victims` 不再逐出全部共享条目（停止条件改用名义字节）；
  `budget.admit()` 拒绝负数；U+2028 分行在三个 env 解析器间对齐；preflight
  接受 NVMe 分区 PIN，且不再把 `[fe80::1]` 视为回环；benchmark.sh 通过临时
  文件传递大提示（env 大小限制）。
* `KVP_L2_SERDE` 不再在 `config.load_config` 中覆盖已导出的
  `LMCACHE_L2_SERDE`（与 identity.py 和 env.sh 的优先级一致）。
* 测试计数：此处 197 unit / 216 offline / 231 共计。recipes 的计数因主机而异
  （torch/lz4/zstd 影响跳过；另一位评审者看到 330+7 对比我们的 332+5）——
  各栏统计的是同一个收集集合，只是 pass/skip 划分不同。

崩溃于 `ModuleNotFoundError: No module named 'openai'` —— 固定的 lmcache
0.5.3 CLI 在注册 bench 子命令时导入 `openai`，因此连 `lmcache server`
没有它也无法启动。`openai>=1.40,<2` 现已固定在 `requirements-lmcache.txt`，
`kvpersist.launch` 打印清晰的安装命令而非裸 traceback。其二：启动器现在
在回环地址上自动生成 API 密钥（保存到 `live/api_key`，mode 600）——与
recipe 启动器相同的契约；wiki 之前要求手动创建密钥。非回环地址仍需显式
密钥。文档一致性：所有安装路径现在使用规范的 `~/sikkatu-recipes` 布局
（之前是 `~/recipes` + 重复的 `recipes/recipes`）。测试：3 个新增
（回环自动生成且 0600 权限、非回环无密钥拒绝且不生成、显式密钥生效）。

raund 34 的门禁只运行 `--check-config-only`，它有意跳过 engine_checks ——
真实的 docker 参数（--kv-cache-dtype、--block-size、TP size）、已连接的
LMCacheMPConnector 以及运行中 LMCache 服务器的 L2 编解码器均未被验证
（zstd 服务器配 raw 命名空间本会通过）。两个 recipe 启动器现在在服务器就绪后、
首个 smoke 请求之前运行完整的 `verify-engine-identity.sh $CONTAINER` ——
MISMATCH（4）/INCONCLUSIVE（5）停止容器并使启动失败（smoke 请求从不发送），
与 `run-vllm-with-persist.sh` 启动后契约完全一致。备用命名空间哈希作为不可达
死代码已删除：kv-persistence（带 KVP_COMPAT_VERSION 和门禁）是 KV_PERSIST=1
的必需依赖。KVP_COMPAT_VERSION 政策：这是接口兼容性编号——凡是 identity 哈希
算法或 CLI 接口变更都必须提升它，否则相同编号的旧克隆会误判为兼容。
测试：6 个新的 recipe 测试（pre-PASS+post-MISMATCH 停止容器且不发送 smoke
请求；pre-PASS+post-INCONCLUSIVE 同理；pre-PASS+post-PASS 发送并成功），
加上本仓库中现有的真实门禁测试（docker 参数 dtype 不匹配 -> exit 4、
l2-adapter codec 不匹配 -> exit 4——它们驱动的是真实门禁而非存根）。

（`run-tp4-uva.sh`、`run-tp4-hotsplit.sh`）此前只打印提醒手动运行
`verify-engine-identity.sh` —— vLLM 可以带着已连接的持久缓存启动而未经验证。
两个启动器现在强制运行门禁（先加载 `live/kvp-effective-identity.env`，
再执行 `verify-engine-identity.sh --check-config-only`）：MISMATCH（exit 4）
或 INCONCLUSIVE（exit 5）阻止启动，缺少门禁脚本则 REFUSED —— 与
`run-vllm-with-persist.sh` 完全相同的策略。另外，跨仓依赖钉住了版本：
kv-persistence 根目录带有 `KVP_COMPAT_VERSION`（当前为 1），三个启动器
都在启动前校验它，拒绝不兼容的克隆（不同版本可能以不同方式计算 identity）。
测试：8 个新的 recipe 测试覆盖 MISMATCH / INCONCLUSIVE / 缺少门禁 /
版本不兼容（每个都证明引擎不会启动）。

`$HOME`/`${HOME}`，而 `config._parse_env_file` 和 bash `env.sh` 会展开——
现在它与其他两个解析器完全一致地展开两种形式。所有三个解析器现在都在
`$HOME` 之前展开 `${HOME}`（规范安全顺序；注意：由于大括号，`$HOME` 实际上
不是 `${HOME}` 的子串，因此旧顺序并未破坏值——经实证验证——但括号优先顺序
同样安全）。一致性测试覆盖两种形式及混合形式，使用传给所有三个解析器子进程
的可控 `HOME`（共 24 个用例）。

接受 Unicode 名称（КЛЮЧ=value 可通过），bash 拒绝——两个 Python 解析器
现在都使用显式 ASCII 正则 `^[A-Za-z_][A-Za-z0-9_]*$`，与 bash 完全一致。
bash 解析器现在也在 `export` 前缀前裁剪行首空白（` export KEY=value`
此前 bash 拒绝但 python 接受）。参数化一致性测试比较完整解析字典
（不只是值），覆盖 20 种输入，包括 Unicode 名称和带前导空格的 export 形式。
测试不会留下被修改的配置文件（备份+恢复；configs/ 的完整临时副本是已
记录的未来改进）。

操作员真实的 `identity.env.local`——此前每次测试运行后都会将其删除
（执行过 setup-identity.sh 的用户在 `pytest` 后丢失身份 pin）。端到端
测试现在诚实地证明 .local 优先于 base（base 声明 lz4，.local 声明 zstd，
无 env 覆盖，适配器为 zstd -> exit 0）。三个 env 解析器（bash env.sh、
config._parse_env_file、identity._read_env_config）现在共享同一严格
格式——名称必须是 ASCII 标识符，值先 trim 再去引号——参数化测试将
18 个相同输入通过全部三个解析器。

在空格或制表符+`#` 处截断尾部注释，与两个 Python 解析器
（config._parse_env_file、identity._read_env_config）逐字节一致——
"MUST match" 契约对 `KEY=value<TAB># comment` 同样成立。新增端到端
测试启动真实门禁，验证 $HOME 展开的 KVP_CACHE_ROOT fallback 并断言
exit 0（.local 优先、适配器发现、编解码器读取）。

lmcache-server.env.local 中的 LMCACHE_* 修改自然生效（此前被默认的
KVP_* 覆盖）。(2) identity.env(.local) 中的 KVP_L2_SERDE pin 现在进入
NAMESPACE（zstd pin -> zstd namespace），消除门禁接受 pin 但 namespace
仍为原始模式的漏洞。(3) run-vllm-with-persist.sh 不再提升裸 BLOCK_SIZE
（保留 round 26 规则）。(4) 门禁在未导出时从 cache.env(.local) 读取
KVP_CACHE_ROOT，配方提醒中提及该变量。(5) README 粘连段落修复（EN），
KVP_L2_SERDE 说明同步到 RU/ZH。

`<cache-root>/l2-adapter.rendered.json`（服务器启动时写入）读取服务器
实际编解码器，而不是门禁自身的 shell 环境——README 中的
`USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh` 恢复正常，
而在另一 shell 中以 zstd 启动的服务器会被抓到（与 namespace MISMATCH）。
KVP_L2_SERDE 现已在 identity.env（EN/RU/ZH）、README、deployment.md 和
配方 tp4.env.example（3 种语言）中说明。run-vllm-with-persist.sh 在
引擎启动前将运维变量名（KV_CACHE_DTYPE、MAX_MODEL_LEN、TP_SIZE、
SERVED_MODEL_NAME）提升为 KVP_* 形式——显式 `KV_CACHE_DTYPE=fp8` 不再
被 identity.env 的 pin 静默忽略（两个启动器对相同环境启动相同引擎）。
systemd 包装器通过带 KVP_* 别名的 repo_cfg 读取 port/L1/chunk/eviction，
cache.env.local 修改在 systemd 下同样生效，CONFIG WARNING 不再对
传播已解决的冲突误报。

KVP_L1_SIZE_GB 值现在会传播到缓存服务器读取的 LMCACHE_* 名称（此前被
静默忽略——服务器使用默认值启动），config.verify() 在启动时报告冲突。
(2) 身份门禁检查 L2 编解码器（l2_serde）：一侧 USE_LOSSLESS_SERDE 与
空 pin 不再静默分歧——不一致即 MISMATCH（exit 4），请将 KVP_L2_SERDE
固定为与服务器一致。(3) 不再读取裸名称 BLOCK_SIZE（配方旋钮为
KV_PERSIST_BLOCK_SIZE）。(4) 配方的 KV_PERSIST_PORT 必须与
kv-persistence 的 LMCACHE_PORT 一致——已在 tp4.env.example（3 种语言）
中说明。(5) run-vllm-with-persist.sh 明确声明运行的是 UVA 基线而非
hotsplit。(6) RECIPE_DIR 现在优先尝试 ~/sikkatu-recipes/recipes。

smoke 请求的 python（shell 赋值未导出，仅设置 KVP_SERVED_MODEL_NAME 时
os.environ 查找会回退到 mimo-pro）。回归测试覆盖该场景以及五参数同时
冲突的组合检查（docker 参数 + namespace + smoke 请求均使用解析值）。

docker 参数、smoke 请求和门禁携带相同的值，因此冲突的 KVP_* 导出
不再能让 namespace/门禁与运行中的引擎脱钩。内联回退与
resolve_effective_identity 一致，优先取 KVP_*。smoke 请求通过 python3
安全编码构建 JSON，并使用实际的 served-model-name。

TP/block-size/served-model-name 默认值）由唯一代码路径解析——
`python3 -m kvpersist.cli effective-identity`——被配方启动器（namespace 与
`live/kvp-effective-identity.env`）、`run-vllm-with-persist.sh` 和标识门禁
共同使用。门禁以同样方式折叠配方的运维变量名（KV_CACHE_DTYPE、
MAX_MODEL_LEN、TP_SIZE、SERVED_MODEL_NAME），因此非默认启动（fp8、
524288、TP!=4）能干净通过验证，而不会因过期 pin 的 MISMATCH 被阻塞。

与 `KVP_TP_SIZE`（kv-persistence）是同一个值——张量并行数——的两个名字。
覆盖时请保持一致（两者都进入缓存命名空间和 `--tensor-parallel-size`）。
非 TP=4 的 hotsplit 运行未经测试：`numa_bind.py` 的 GPU 映射示例按 4 卡计算，
任何 TP != 4 都是未经验证的领域。

中明确记录并附「两侧同步修改」警告。

**仍未证明的部分（诚实声明）**：配方的契约测试只验证启动器包含该机制 —— 它们不能
证明真实的 NVMe 恢复、跨重启复用或跨配置隔离。这些证据来自 kv-persistence 的
`scripts/test-recovery.sh` + GPU 主机上的集成测试。硬件验收仍未完成。

## 第 20 轮评审修正（跨仓库命名空间一致性 + 诚实的默认值）

**P1 —— 命名空间不跟随引擎的实际参数**（评审者验证：tp4.env 中 `KV_CACHE_DTYPE=fp8`
仍沿用固定的 `bfloat16` 管脚 —— fp8 KV 挂在 bf16 命名空间下，即**错误命中**风险，
而非安全未命中）：配方的 `KV_PERSIST=1` 分支现在把实际引擎值作为
`KVP_KV_CACHE_DTYPE`、`KVP_MAX_MODEL_LEN`、`KVP_BLOCK_SIZE`、`KVP_TP_SIZE` 覆盖
传给 `kvpersist.cli model-name`（env 在 `identity_from_env_files` 中优先于文件），
且 `auto` 先解析为模型的有效 dtype（config.json `torch_dtype`/`dtype`）再计算命名
空间 —— 因此同一模型的 `auto` 与显式 `bfloat16` 启动落入同一命名空间，而 `fp8` 落入
自己的。备用哈希（未安装 kv-persistence 时）也补充了 tokenizer 文件、chat template
和 TP size —— 旧版三者皆缺。「never a wrong read」替换为精确表述（命名空间来自实际
引擎参数 ⇒ 安全未命中），同步更新 README ×3、tp4.env.example ×3 与 wiki。

**P2 —— CLI 错误被吞掉**（`2>/dev/null || true` + 误导性的
「kv-persistence not found」fallback —— 正是评审者在默认空 pin 上遇到的）：
CLI 的 stderr 现被捕获并原样打印，不可用的身份会**中止启动**（`die`）并指向
`setup-identity.sh` —— 错误的命名空间绝不允许猜测进入生产。

**P3 —— 「两条路径，一个缓存」默认为假**：配方传 `KVP_SERVED_MODEL_NAME=mimo-pro`，
而 kv-persistence 启动器用 `basename(MODEL_DIR)`（如 `MiMo-V2.6-Pro-MOPD`）——
相同哈希、不同前缀、缓存永不共享（评审者实测 `mimo-pro/d2089a2f…` vs
`MiMo-V2.6-Pro-MOPD/d2089a2f…`）。专用启动器的默认值现跟随 `SERVED_MODEL_NAME`
（引擎实际调用名），两条路径得到相同 `model_name`。由
`tests/unit/test_launcher_namespace_coherence.py`（执行真实默认值表达式）与
`test_served_model_name_prefix_is_stable` 覆盖。

**P4 —— 门禁把 `block_size=16` 当事实代入**：固定版本的 `DEFAULT_BLOCK_SIZE` 是 16，
但 `platforms/interface.py` 可经 `get_preferred_block_size` 选择其他值，引擎会记录
「Setting kv cache block size to N」。门禁不再代入 16：先读 `config.json`
（`block_size`/`hybrid_block_size`），再读容器引擎日志行，两者皆无则报 INCONCLUSIVE
（无法观察的事实不得宣称）。`KV_PERSIST=1` 分支仍固定 `--block-size`（现经命名空间化
旋钮 `KV_PERSIST_BLOCK_SIZE`，顺带解决 `BLOCK_SIZE` 环境冲突）。

**P5 —— 配方默认值对所有运行生效**（「byte-identically to before」为假）：
`MAX_NUM_SEQS` 1→2，`KV_CACHE_DTYPE` 恢复为 `auto`（已验证的基准测试跑在 auto）。
按操作员决定 `MAX_NUM_SEQS=2` **保留**为默认（无并行请求时速度不受影响，更方便），
故旧表述替换为 README ×3 / tp4.env.example ×3 中的显式提示：两个默认值对所有运行
生效，以及如何精确复现旧基准条件。

**测试现在执行该块**（评审者指出文本搜索测试什么都没抓到）：
`tests/test_kv_persist_behaviour.py` 用 `docker`/`nvidia-smi`/`curl`/`python3` 桩
实际运行两个启动器，断言：命名空间跟随真实引擎参数（fp8 与 bfloat16 不同）、CLI 失败
以真实原因中止、`KV_PERSIST_NAMESPACE` 覆盖被尊重、普通启动不派生任何命名空间、
`KV_PERSIST_BLOCK_SIZE` 到达引擎、model_name 前缀稳定、`auto` 解析为模型 dtype。
开发中它抓到两个真实 bug（CLI 调用路径里未声明的 `ROOT` 变量、命名空间前缀失配）。