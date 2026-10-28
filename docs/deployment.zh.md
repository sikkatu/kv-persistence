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

## 6. 启动带缓存的 vLLM

```bash
export IMAGE='…@sha256:<digest>'
export MODEL_DIR="$HOME/models/<your-model-dir>"
bash scripts/run-vllm-with-persist.sh
```

## 7. 验证

```bash
bash scripts/verify-engine-identity.sh      # 强制门禁：引擎几何 == 身份固定
bash scripts/test-recovery.sh               # 验收：见 docs/benchmarks.md
```

## 回滚（一条命令）

```bash
bash "$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh"
```

生产配方从未修改；NVMe 上的持久 KV 数据不删除，仅被忽略。显式清除：
`bash scripts/budget-report.sh purge --all --yes`（仅手动执行）。