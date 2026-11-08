# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 58 轮（统一配置层 + 共享密钥回归修复）

* 共享 API 密钥回归已修复（recipes）：启动器现在每次启动都将规范文件
  `$HOME/.config/kvp/api_key` 镜像到 `live/api_key`，因此 `manage.sh` /
  `check-tp4-api.sh` 不会在密钥存在时报「需要密钥」。全部五个配方启动器
  使用同一密钥架构；辅助脚本优先读取规范文件且不再依赖 `tr`/`head`
  （纯 bash 读取）。
* 统一编解码决策：`start-all.sh` 只解析一次有效 L2 编解码（共享折叠），
  并将同一值传给缓存和引擎——identity.env 中的编解码钉定不会再让两侧
  使用不同编解码。
* 统一配置层：`start-all.sh` 在任何决策之前加载 cache.env /
  lmcache-server.env / identity.env——缓存 PID 探测使用有效的
  `KVP_CACHE_ROOT`，自定义路径上的缓存会被识别为正在运行。
  `run-vllm-with-persist.sh` 现在读取配方的 `tp4.env`（环境变量优先于
  文件），因此通过 `start-all.sh` 启动时 HOTSPLIT_* / MAX_MODEL_LEN /
  MAX_NUM_SEQS / MOE_BACKEND 能到达引擎。
* 统一入口：recipes 中的 `scripts/start-all.sh` 委托到 kv-persistence
  —— 同一命令可在任何工作目录下运行。`run-tp4-hotsplit.sh` 仍是配方的
  独立启动器。
* 跨仓库契约测试（`tests/test_cross_repo_contract.py`，recipes）：
  编解码语义、lmcache 版本钉定（0.5.6）、端口、namespace 钉定和挂载的
  HotSplit 文件必须在两个仓库中一致——一个仓库的更改不会再悄悄破坏另一个。
* 压缩文档统一（EN/RU/ZH + 配置注释）：无损 zstd 到处是默认值，lz4 是
  备选，逐字节精确原始模式仅为显式关闭（`LMCACHE_L2_SERDE=none`）。

## 第 57 轮（判定 + 文档诚实性）

* 恢复验收判定在 docs/final-report.* 中为 **PASS**：缓存设备自动检测
  （第 55 轮）使独立 NVMe 字节计数器在同一轮可用——无需手动
  `KVP_CACHE_DEVICE`（覆盖项仍可用）。
* CHANGELOG 不再包含逐字请求引用——变更直接描述。
* `start-all.sh` / `stop-all.sh` 和共享 API 密钥（`$HOME/.config/kvp/api_key`）
  的文档已添加到两个仓库的 README（EN/RU/ZH）。

## 第 56 轮（共享 API 密钥 + 默认动态 hotsplit）

* 两个仓库共用一个 API 密钥：两个启动器（kv-persistence + recipes 的 run-tp4-hotsplit.sh /
  run-tp4-uva.sh）现在读写规范文件 `$HOME/.config/kvp/api_key`
  （覆盖：`KVP_API_KEY_FILE=`）。旧版 `live/api_key` 首次使用时自动迁移，
  并保留镜像供旧工具使用。
* kv-persistence 默认启用动态热/冷专家：启动器现在挂载配方的 `/w` 机制（hotsplit.py、hotsplit_plan.py、
  numa_bind.py、sitecustomize.py、expert_hist_mix.json）并传入
  `PYTHONPATH=/w` 和配方的 HOTSPLIT_* 默认值（HOTSPLIT_DYNAMIC=1、
  HOT_GIB=30）。关闭：`KVP_HOTSPLIT=0`。
* CHANGELOG 精简为最近 3 轮——较早的记录见 `main` 分支的 git 日志。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
