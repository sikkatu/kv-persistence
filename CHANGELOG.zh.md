# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 56 轮（共享 API 密钥 + 默认动态 hotsplit）

* 两个仓库共用一个 API 密钥（用户：“两个仓库的 api 密钥不一样”）：
  两个启动器（kv-persistence + recipes 的 run-tp4-hotsplit.sh /
  run-tp4-uva.sh）现在读写规范文件 `$HOME/.config/kvp/api_key`
  （覆盖：`KVP_API_KEY_FILE=`）。旧版 `live/api_key` 首次使用时自动迁移，
  并保留镜像供旧工具使用。
* kv-persistence 默认启用动态热/冷专家（用户：“我们默认用动态专家吗？”）：
  启动器现在挂载配方的 `/w` 机制（hotsplit.py、hotsplit_plan.py、
  numa_bind.py、sitecustomize.py、expert_hist_mix.json）并传入
  `PYTHONPATH=/w` 和配方的 HOTSPLIT_* 默认值（HOTSPLIT_DYNAMIC=1、
  HOT_GIB=30）。关闭：`KVP_HOTSPLIT=0`。
* CHANGELOG 精简为最近 3 轮——较早的记录见 `main` 分支的 git 日志。

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
* 机器判定当时仅因未设置 KVP_CACHE_DEVICE 而为 INCONCLUSIVE
  （第 55 轮已用自动检测修复）。
* README（EN/RU/ZH）、兼容性矩阵和 final-report 已更新：ACCEPTED 状态附证据。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
