> 🌐 本页其他语言版本：[English](benchmarks.md) | [Русский](benchmarks.ru.md) | **中文**

# 基准测试

测量规则：每个数字都必须包含请求条件（冷/热、进程状态、缓存状态）。
没有磁盘恢复证据的加速**不**满足主里程碑。

## 测试证据级别（每个测试证明什么）

三个级别严格区分——绝不把一个当成另一个（review P1-2）：

| 级别 | 测试 / 脚本 | 证明 | 不证明 |
|---|---|---|---|
| 1. 文件级冒烟测试 | `tests/integration/test_l2_persistence.py::test_l2_file_survives_server_restart` | 手写文件能存活进程终止 | LMCache 能重新读取自身对象 |
| 2. LMCache 持久化往返 | `tests/integration/test_l2_persistence.py::test_lmcache_store_restart_retrieve_roundtrip` | LMCache 通过真实 MP 协议写入，第一个服务器进程完全停止，同一 L2 目录上的**新**进程检索出重启前的对象且分块校验和一致 | vLLM + MiMo-V2.6-Pro 在真实对话中复用缓存 |
| 3. vLLM 完整验收 | `scripts/test-recovery.sh`，`tests/recovery/` | 真实 vLLM 负载在预期重启后复用持久化 KV（项目验收标准） | — |

### 级别 2：LMCache 持久化往返（合成 KV，无 vLLM，无 GPU）

该测试证明 **LMCache 本身**能在服务器完全重启后从 L2 层恢复数据。
不需要 vLLM、模型或 GPU。

* **固定版本 / API**：LMCache **0.5.3**（固定于 `requirements-lmcache.txt`）。
  测试通过固定 CLI `lmcache bench server`（`--mode cpu --transfer-mode
  engine_driven`）驱动真实 MP 服务器协议：`REGISTER_KV_CACHE_ENGINE_DRIVEN_CONTEXT`、
  `LOOKUP`、`QUERY_PREFETCH_STATUS`、`PREPARE_STORE`/`COMMIT_STORE`、
  `PREPARE_RETRIEVE`/`COMMIT_RETRIEVE`（ZMQ）。缓存标识基于 token 哈希，
  跨调用确定（`IPCCacheServerKey.request_id` 被排除在键相等性之外），
  因此相同的负载标识在后续运行中寻址相同的对象。每分块 KV 字节的 MD5
  以 `CHECKSUM: <digest> (N chunks)` 输出。注意：`--l2-adapter` 接收
  JSON **字符串**形式的适配器规格，而非文件路径（0.5.3 `add_l2_adapters_args`）。
* **依赖**：Python 3.12、支持 POSIX SHM 的 Linux（`/dev/shm`）、固定的
  `lmcache==0.5.3` 加 `nixl>=1.3.0`（`nixl_store_dynamic` L2 适配器导入
  `nixl`）、torch（CPU 构建即可）、pytest。**不需要** GPU、CUDA 和 vLLM。
* **命令**：
  ```bash
  python3 -m venv ~/.venvs/lmcache
  ~/.venvs/lmcache/bin/pip install -r requirements-lmcache.txt 'nixl>=1.3.0'
  export PATH="$HOME/.venvs/lmcache/bin:$PATH"
  KVP_LIVE_LMCACHE=1 python3 -m pytest tests/integration/test_l2_persistence.py \
      -k roundtrip -v
  ```
* **测试内容**：将仓库的 L2 适配器模板渲染到隔离的 `tmp_path` 缓存根目录
  （保留生产设置：POSIX 后端、`persist_enabled=true`），在动态分配的端口上
  启动真实 `lmcache server`，通过真实协议存储确定性的多分块合成 KV 负载，
  等待适配器自身的 `*.bin` 数据文件出现，完全终止服务器进程，在同一 L2 目录上
  启动**全新**进程并检索重启前的对象——重启后零次新写入。warm-pass 分块校验和
  （在客户端缓冲区清零并由 RETRIEVE 写回数据后计算）必须等于重启前的
  ground-truth 校验和。
* **PASS 证据**：新服务器上 `LOOKUP: N/N chunks hit`、`RETRIEVE: retrieved`、
  重启后没有 `STORE: stored` 行、匹配的 `CHECKSUM` 摘要，以及阴性对照
  （相同负载、空 L2 目录）报告 `0/N` 分块——完全命中不可能是假阳性。
* **故障排查**：每个服务器的 stdout/stderr 和每次 bench 输出都追加到
  `<tmp_path>/roundtrip.log`；失败消息指明失败步骤（启动 / 写入 / 停止 /
  新启动 / 缓存未命中 / 检索 / 校验和不匹配）并以日志结尾。用 `-s` 重跑
  可看到 PASS 摘要，或用 `pytest --basetmp=/some/dir` 保留临时目录。
* **范围限制**：负载是合成 KV 张量。该测试仅证明级别 2；vLLM +
  MiMo-V2.6-Pro + `MAX_NUM_SEQS=2` 的 Operit 对话场景须由
  `scripts/test-recovery.sh`（级别 3）单独证明。

## 矩阵（5 种配置 × 3 种提示规模）

| # | 配置 | 隔离什么 |
|---|---|---|
| 1 | 基线：仅内存前缀缓存 | 参考 TTFT/prefill |
| 2 | 持久缓存冷启动 | 连接器开销 |
| 3 | 持久缓存热，同进程 | L1/L2 命中 |
| 4 | vLLM 重启，缓存服务存活 | L2 → L1 恢复 |
| 5 | **两服务都重启**，NVMe 数据保留 | **完整持久化（验收标准）** |

提示词规模：合成 32K/128K/256K token + 真实长对话（`prompt_probe.py` 先测量
稳定前缀）。1M 提示在硬件有可行性时才执行。

## 每次运行的指标

prompt_tokens、cached_tokens（来自 usage.prompt_tokens_details）、ttft_s、
total_s、缓存字节变化（budget-report.sh status）、LMCache 行数、VRAM/RAM 峰值、
错误/回退。

## NVMe 读取的物理证据（不只是日志文本）

日志解析（`Retrieved`/`L2` 等词）具有版本依赖性，审查指出这是误报风险。
因此 `test-recovery.sh` 用独立物理信号佐证日志证据：共享 VM 磁盘（同盘还有 Docker 与模型权重） 设备
（`KVP_CACHE_DEVICE`，从 `/sys/block/<dev>/stat` 字段 3 读取）在每个场景期间
的读扇区计数器。若场景显示有复用但物理 NVMe 读取为 0，则不能归功于 L2。

KVP_CACHE_DEVICE 未设/计数器不可读 = 运行结果 **INCONCLUSIVE**（退出码 5）：
既非 PASS 也非 FAIL，因为未经证实的磁盘来源绝不能当作成功。

## 缓存结果分类学

成功的生成 ≠ 成功的缓存。每次运行把每个请求归入**唯一**一种结果：

| 结果 | 所需证据 | 算成功吗？ |
|---|---|---|
| **PERSIST 命中** | 重启后 L2 读取事件 + cached_tokens > 0 + 无重写 | **是** —— 项目目的 |
| 内存命中（L1/RAM） | cached_tokens > 0，但无重启后 L2 证据 | **否**（不证持久化） |
| 普通前缀缓存命中 | cached_tokens > 0，无 LMCache 事件 | **否** |
| 缓存未命中（重算） | 无复用，全新 prefill | 冷运行预期 |
| 不兼容数据 | 不同命名空间，干净未命中 | 安全行为 |
| 读取/恢复错误 | 适配器/连接器错误 | **否** —— 硬失败 |

## 判定标准

PASS 需要**全部**满足：
1. 重启后请求 cached_tokens > 0（测试 4 和 5）；
2. LMCache 日志显示重启后 L2/磁盘读取事件；
3. TTFT/prefill 相比可比冷基准显著下降；
4. 回答文本正确；
5. NVMe 缓存字节跨重启不变（证明数据被复用而非重写）。