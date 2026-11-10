> 🌐 本页其他语言版本：[English](deployment.md) | [Русский](deployment.ru.md) | **中文**

# 部署

线性路径 —— 没有任何需要手动补充的步骤。命令设计为逐字复制到 4-GPU 主机
（Debian）。主机运行 sikkatu/recipes 中的生产配方；本项目不修改它。

## 0. 前提条件（主机）

* Linux 主机（任意发行版），配备 NVIDIA GPU、Docker、nvidia-smi、固定的 vLLM 镜像
  （IMAGE，含 @sha256: 摘要）和模型目录 MODEL_DIR。
* 本地 NVMe 在固定设备（preflight 通过 KVP_CACHE_DEVICE 校验）上有至少
  KVP_CACHE_BUDGET_GB 可用空间（默认 1.5 TB）。
* 生产配方已就位；脚本仅使用其 tp4.env.example（IMAGE 摘要）与
  patches/ + MANIFEST.sha256（只读挂载）。
* Python 3.12（用于 kvpersist 管理 + 脚本与 lmcache 的 CLI）。
  Debian/Ubuntu (tested on Debian 12) 系统 Python 为 3.11：单独安装 Python 3.12 解释器
  （如 bookworm-backports 或 pyenv），python3-venv 需单独安装。

## 1. 克隆 kv-persistence

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
export REPO="$PWD"
```

导出变量（REPO、IMAGE、MODEL_DIR）只存活于原 shell 会话；新终端请重新导出。

## 2. 配置

站点本地覆盖写入 *.local 文件（优先于内置默认，已 gitignore）；
setup-identity.sh 写入 identity.env.local，绝不修改追踪的 identity.env：

```bash
cp configs/generic/cache.env configs/generic/cache.env.local
$EDITOR configs/generic/cache.env.local        # KVP_CACHE_ROOT 必须位于 NVMe
```

身份值由一条命令自动填写并核验：

```bash
bash "$REPO/scripts/setup-identity.sh" "$HOME/models/<your-model-dir>"
```

REV 身份只有两种格式（每个部署用且仅用其一）：
1. 本地缓存指纹（按 shard 内容哈希）—— 存储安全的默认选择。
2. HF 提交 SHA —— 仅当有意追踪 HF 来源；注意 HEAD 可能已不是本地文件版本。

## 3. 安装 LMCache CLI

```bash
python3 -m venv ~/.venvs/lmcache
~/.venvs/lmcache/bin/pip install --upgrade pip
~/.venvs/lmcache/bin/pip install -r "$REPO/requirements-lmcache.txt"
export PATH="$HOME/.venvs/lmcache/bin:$PATH"
```

版本已固定（lmcache==X.Y.Z）并与 vLLM 镜像组合记录。安装成功 ≠ ABI 兼容已证明：
最终验收需要 lmcache server --help、包导入检查和目标硬件上的完整恢复场景。

**关于固定 CLI 0.5.6 的说明**：`lmcache server --l2-adapter` 接收 JSON
**字符串**形式的适配器规格，而不是 JSON 文件路径。`start-cache.sh` 会自动
处理（内联传入渲染后的规格），但手动启动服务器时请传
`--l2-adapter "$(cat l2-adapter.rendered.json)"`，而不是
`--l2-adapter l2-adapter.rendered.json`。

## 4. 预检

```bash
bash scripts/preflight.sh
```

preflight PASSED 即可继续。每个 [!!] 都指出需要修复的内容。设置 KVP_MODEL_DIR
后预检还会按统一算法重新计算 chat-template 摘要并与配置对比。

## 5. 启动持久缓存服务

```bash
bash scripts/start-cache.sh
```

创建 KVP_CACHE_ROOT（chmod 700），渲染 L2 适配器规格并启动
lmcache server（setsid nohup，日志和 pid 在 KVP_CACHE_ROOT 下）。
已有缓存数据会被保留 —— 启动绝不自动清空缓存。

### L2 无损编解码（可选）

默认情况下 L2 层存储**逐字节精确的原始**块（`LMCACHE_L2_SERDE=none`）
—— 与未压缩的 upstream 路径完全一致，不做任何变换。若要以无损压缩存储
（幅度温和，真实 KV 负载约 1.1–1.4x），请选择编解码：

```bash
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh     # zstd
USE_LOSSLESS_SERDE=lz4 bash scripts/start-cache.sh   # lz4
```

或在 `configs/generic/lmcache-server.env(.local)` 中永久设置：
```
LMCACHE_L2_SERDE=zstd     # 或 lz4，或 none
```
本项目**不存在有损量化**（fp8 / turboquant / lbq 在启动时被明确拒绝）。
编解码属于缓存身份命名空间的一部分：切换编解码会让已存储的块变成安全的
cache miss（绝不会错误命中），因此请在所有启动器中使用同一取值。
`KVP_L2_COMPRESS_LEVEL`（仅 zstd，默认 3，范围 1-19）用于权衡压缩率与
写入速度。

### 启动模式 A（推荐）：same-UID，无需 root（第 45h 轮）

不必用 sudo——两个进程可以共享同一个 UID：引擎容器以
`--user $(id -u):$(id -g)` 启动（动态：适用于任何用户、任何 UID——
绝无硬编码）。CUDA IPC 测试已验证此路径
（`PRODUCER_USER=$(id -u):$(id -g) bash scripts/ipc-test.sh` = PASS）。

```bash
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
USE_LOSSLESS_SERDE=1 bash scripts/start-all.sh
```

此模式下 HF 缓存挂载到 `/tmp/hf-cache`，torch 缓存
（`XDG_CACHE_HOME`、`TRITON_CACHE_DIR`）移到 `/tmp` 下——非 root 用户
无法写 `/root`。预检 IPC 测试以 `PRODUCER_USER=$(id -u):$(id -g)`
运行，恰好复现生产边界。缓存服务器以您的用户运行（无需 sudo），
缓存文件归您所有。

### 启动模式 B（备选）：均以 root 运行（第 45 轮）

CUDA IPC 边界测试（`bash scripts/ipc-test.sh`）在目标主机上证实：引擎容器
（root）创建的 GPU 内存只有主机上的 ROOT 能打开。非 root 的缓存服务器会在
`register_kv_caches` 处以 `CUDA_ERROR_MAP_FAILED` 失败。请用 sudo 启动两个
服务：

```bash
sudo USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
USE_LOSSLESS_SERDE=1 bash scripts/start-all.sh
```

`start-cache.sh` 在 sudo 下自动解析 lmcache venv 的 python
（`SUDO_UID`/`SUDO_USER`——无需手动 export）。缓存文件将属 root：
`budget-report.sh` / `purge` 也请用 sudo 运行。

**选择一种模式（A 或 B）并一致使用**——引擎和缓存服务在 CUDA IPC 边界必须
看到相同的 UID。模式 A 无需 root，为推荐默认；模式 B 是无法以非 root 启动时
的备选。

## 6. 启动带缓存的 vLLM

```bash
export IMAGE='…@sha256:<digest>'
export MODEL_DIR="$HOME/models/<your-model-dir>"
KVP_SAME_UID=1 bash scripts/start-all.sh   # 模式 A（无需 root）
# 模式 B（root）：sudo KVP_SAME_UID=0 bash scripts/start-all.sh
```

## 7. 验证

```bash
bash scripts/verify-engine-identity.sh      # 强制门禁：引擎几何 == 身份固定
bash scripts/test-recovery.sh               # 验收：见 docs/benchmarks.md
```
## 监控
```bash
bash scripts/budget-report.sh status      # 占用、余量、磁盘
bash scripts/budget-report.sh quota       # 各命名空间配额
bash scripts/budget-report.sh purge --help
```
### Prometheus + Grafana（可选）

完整可观测性栈（OTel Collector + Tempo + Prometheus + Grafana）位于
`observability/`，已预置官方 LMCache 仪表盘：

```bash
bash scripts/start-observability.sh
# Grafana -> http://localhost:3000（匿名 Admin，无需登录）
```

然后把缓存服务指向收集器以使指标流动
（`KVP_OTLP_ENDPOINT=http://localhost:4320`，或简写 `USE_OBSERVABILITY=1`）。
遥测默认关闭：没有该栈缓存也完整工作。详情与面板清单见
`observability/README.zh.md`。
## 在 systemd 下运行缓存服务（可选）

不用 `scripts/start-cache.sh`（`setsid nohup`）时，缓存服务可作为 systemd
单元运行——可跨重启存活、失败自动重启、日志进 journald。单元调用
`kvp-lmcache-server` 包装脚本，以 JSON 字符串传入 L2 适配器规格（锁定的
LMCache 0.5.6 CLI 的 `--l2-adapter` 接受 JSON 字符串，而非文件路径）。

```bash
sudo install -m 0755 systemd/kvp-lmcache-server /usr/local/sbin/kvp-lmcache-server
sudo install -m 0644 systemd/lmcache-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lmcache-server
journalctl -u lmcache-server -f
```

* 包装脚本在渲染文件不存在时（干净主机）从
  `configs/${KVP_MODEL_CONFIG:-mimo-v2.6-pro}/l2-adapter.json.in` 渲染规格，
  因此单元可以先于第一次 `start-cache.sh` 启动。若仓库路径不同于
  `/home/kvp/kv-persistence`（README 克隆到 `~/kv-persistence`），请在
  `/etc/kv-persistence.env` 中设置 `KVP_REPO_ROOT`。
* 包装脚本读取 `cache.env`/`.local` 中的 `KVP_CACHE_ROOT` 与预算，与
  `scripts/env.sh` 优先级一致（env 优先于文件）——`cache.env.local` 中的
  自定义缓存根目录会被尊重。
* 单元以用户 `kvp` 运行；按实例用户请用模板单元 `lmcache-server@.service`
  （`User=%i` 仅在模板单元中合法）。
* **缓存根目录权限**：服务用户必须有写权限。默认路径 `/var/lib/kv-persistence`
  由单元的 `StateDirectory=kv-persistence` 创建并设置正确属主。自定义
  `KVP_CACHE_ROOT` 时执行一次：
  ```bash
  sudo install -d -o kvp -g kvp -m 0700 /mnt/nvme/kvp   # 示例路径
  ```
  包装脚本在启动时也会检查可写性，若目录不可写则打印这条命令。
* 不要同时运行单元和 `start-cache.sh`——会争夺同一缓存目录和端口。

## 停止（引擎与缓存服务）
引擎和缓存服务是不同的进程，请分别显式停止：

* **vLLM 引擎**（`run-tp4-hotsplit.sh` 启动的容器）：
  `bash scripts/stop-engine.sh`（或 `docker rm -f mimo-pro-tp4-hotsplit`）。
  在启动器运行中按 Ctrl-C 只会停止启动器本身——容器会继续运行；
  之后请执行 `stop-engine.sh`。
* **缓存服务**（lmcache 服务器）：`bash scripts/stop-cache.sh`
  （优雅 SIGTERM，数据文件保留）。

停止任意一个都不会删除磁盘上的持久缓存数据。

**重启顺序（第 46 轮，审阅者）：** 只在引擎已停止时重启缓存服务。如果引擎崩溃
（exit 1 / OOM），缓存服务器也必须重启——它可能仍持有死去引擎的 GPU/主机内存，
在重启它之前单纯重启引擎会失败。正确顺序：`bash scripts/stop-engine.sh` ->
`bash scripts/stop-cache.sh` -> `bash scripts/start-cache.sh` ->
`bash scripts/start-all.sh`。启动器的 GPU 预检
（`scripts/env.sh` 中的 `_kvp_gpu_preflight`）在 VRAM 被占用时拒绝启动并打印
占用者 PID。

## 回滚（一条命令）

```bash
bash "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh"
```

生产配方从未修改；NVMe 上的持久 KV 数据不删除，仅被忽略。显式清除：
`bash scripts/budget-report.sh purge --all --yes`（仅手动执行）。