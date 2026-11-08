# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


## 第 70 轮（委托：KVP_ENGINE=recipe，默认关闭）

* 一个引擎启动，两条路径：`start-all.sh` 现在支持 `KVP_ENGINE`
  （`kv` — kv 启动器，默认；`recipe` — 经硬件验证的
  `run-tp4-hotsplit.sh`，带 KV_PERSIST=1）。默认关闭，旧引擎代码在硬件
  运行确认 recipe 路径之前不受影响——完全符合审阅要求。
* 审阅者的步骤 2 清单，全部接入并测试：
  * 没有标志时行为不变（docker run 契约快照固定行为；默认路径测试断言
    运行的是 kv 启动器）；
  * 单一容器名：`start-all` 将有效名称记录到
    `live/engine_container`；`stop-engine.sh`（kvp down / stop-all.sh）
    和 `kvp logs/status` 读取它——停止的正是启动的那个（recipe 将容器
    命名为 mimo-pro-tp4-hotsplit，kv 启动器为 mimo-pro-tp4-kvp）；
  * 单一密钥来源：共享的 `~/.config/kvp/api_key` 以 API_KEY 导出到
    环境变量——绝不进入 argv——recipe 启动器复用它且不会生成第二个；
  * KV_CACHE_DTYPE：固定为 bfloat16（recipe 默认 auto）。差异在测试中
    明确展示，绝不被规范化隐藏；identity 哈希考虑 dtype（每种 dtype
    -> 独立命名空间；auto->bfloat16 解析契约在启动器源码中断言）；
  * 退出码：recipe 启动器在前台运行——它的 exit 1（激活检查）原样传到
    start-all.sh / kvp up（已测试：缺少标记 -> exit 1）。
* `tests/test_engine_delegation.py`（6 个测试）覆盖清单；强制的
  identity-gate 被桩替换（与 test_kv_persist_behaviour 相同的装置）——
  真正的 identity 属于 kv-persistence 的领域。

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

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
