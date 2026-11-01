> 🌐 本页其他语言版本：[English](final-report.md) | [Русский](final-report.ru.md) | **中文**

# 最终报告 — kv-persistence

仓库：github.com/sikkatu/kv-persistence（`main` 分支，提交 `HEAD`（见 `git rev-parse HEAD`） —— 已通过 GitHub remote API 验证）。

## 总状态：PARTIAL

实现已完成，离线测试全绿（59 个单元测试，含 3 个 P0 权重哈希回归、配置一致性、预算和保留策略）。
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
  固定值与身份门禁均使用 bf16；声称「目标是 fp16」的注释已更正
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