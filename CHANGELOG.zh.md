# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 69 轮（步骤 2 准备：recipe 启动器激活检查、契约快照）

* recipe 启动器（`run-tp4-hotsplit.sh`）现在在宣布成功前执行与 kv 引擎
  启动器（第 59 轮）相同的 hotsplit/NUMA 激活检查：boot-hook 标记（ACTIVE
  或使用模型补丁时的有意 INACTIVE）、`step hook installed`、
  `live counter:`、NUMA 启用时的 `HOOK STATUS numa_bind=ACTIVE`、无空 bank、
  无事务错误。缺少标记即 exit 1。这在 `KVP_ENGINE=recipe` 出现之前就
  堵住了「委托会丢失检查」的缺口。关闭：`KVP_SKIP_HOTSPLIT_CHECK=1`。
* docker run 契约快照（`tests/fixtures/launcher-docker-run.txt`，测试
  `test_snapshot_contract`）：没有委托标志时最终的 `docker run` 必须与
  记录的契约逐字节一致。仅故意更新：`KVP_UPDATE_SNAPSHOT=1`。
* 激活负向测试（`tests/test_activation_negative.py`）：缺少标记 -> exit 1
  并列出每个 `[MISSING]` 行；标记完整 -> exit 0；跳过开关有效。启动器的
  返回码就是 start-all.sh / kvp up 看到的（它们在前台运行它）。
* 旧测试套件使用已文档化的 opt-out（它们的 docker 桩不输出标记且断言
  其他行为）；检查本身由专用测试覆盖。

## 第 68 轮（lz4 边界修复、行为级解析器测试）

* lz4 边界 bug（审阅者发现）：`max_length=cap - len(out)` 在上限处变成 0——
  而在 lz4 流式 API 中 0 表示无限制，100 MiB 炸弹在一次无界调用中返回。
  已修复为 `cap + 1 - len(out)`（当 len(out) <= cap 时永不 <= 0；多余字节
  触发溢出检查）。诚实帧不受影响。转发测试的桩现在记录每次调用并在炸弹、
  边界和诚实路径上断言 max_length > 0 且 chunk <= cap+1。
* 行为级解析器测试（审阅者发现：子字符串检查无法捕获漂移）：
  `tests/test_parser_behaviour.py` 在同一个 tp4.env 上运行两个解析器
  （kv-persistence env.sh 和配方的 kvp-common.sh）并逐键比较结果环境。
  测试用例覆盖所有棘手规则（$HOME 展开、引号、行内注释、值中的 #、
  export 前缀、缩进注释、无效标识符）。测试立即捕获了一个共同的语义
  缺陷：`KEY = value` 在值中保留了前导空格——两个解析器现在都去除前导
  和尾随空白（5 个文件同步更新；逐字节一致性契约测试保证同步）。
* 真实编解码器仍未验证（CI 中没有 zstandard/lz4）：zstandard 在头部大小
  超过 max_output_size 时的确切行为、lz4 的真实边界语义。在安装了包的
  主机上 `pytest tests/unit/test_decompress_bomb.py -rs` 应显示 0 个
  SKIPPED。

## 第 67 轮（codec cap = plain_len、统一 shell 库、对等三要素）

* 解压上限已修复（审阅者的发现）：编解码器输出界限现在是帧的
  **plain_len**（已与目标缓冲区和 KVP_DECOMPRESS_MAX_BYTES 上限校验），
  而不是上限值。向下撒谎的帧（声明 2 KiB，内部是炸弹）不能再先膨胀到
  上限。plain_len == 0 获得 1 字节界限（zstandard 将 max_output_size=0
  读作「未设置」）。通过间谍测试（断言编解码器收到 plain_len）和空帧
  测试验证。
* 统一 shell 库：每个配方的 `scripts/kvp-common.sh` 包含 `kvp_curl`
  （管道传密钥、已转义）和 `_kvp_seed_env_file`（tp4.env 解析器）。
  12 个内联 `kvp_curl` 副本和 2 个内联解析器块已删除——每个脚本都
  source 共享文件。配方保持独立（每配方一份副本），
  `test_cross_repo_contract.py` 强制副本逐字节相同；第二个测试保持
  kv-persistence scripts/env.sh 函数同步（转义行必须完全一致）。
* 对等测试现在在两个启动器上检查审阅者明确要求的三要素：`/live` 计数器
  挂载、`--env-file` 密钥传输和 argv 中无密钥。加上共享的 HOTSPLIT_* 环境
  和 caps/compilation-config 配置。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
