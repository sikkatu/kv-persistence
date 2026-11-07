# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


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

## 第 55 轮（KVP_CACHE_DEVICE 自动检测）

* `KVP_CACHE_DEVICE` 无需手动猜测（VM + LUKS 友好）：env.sh 中的共享助手 `_kvp_resolve_cache_device` 从 `live/cache_device`
  （记住）或缓存根的 `findmnt SOURCE` 解析后备设备——支持 VM 磁盘
  （`/dev/vda1`）、LUKS mapper 节点（`/dev/mapper/cryptroot`，经 readlink）
  和 NVMe 分区。test-recovery.sh 与 preflight.sh 使用；显式固定仍优先。
  选择记录在 `live/cache_device`。
* docs/final-report.md 余项清理：LMCache runtime、version-pin 和
  serde end-to-end 行现为 VERIFIED（硬件 2026-11-07）。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
