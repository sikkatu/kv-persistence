# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


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

## 第 66 轮（小修复 + 解压上限 + 启动器对等）

* 小修复（按审阅者要求单独提交）：test-recovery.sh 的密钥优先级现在是
  环境 > 共享规范密钥 > 本地镜像（显式 API_KEY 永不被覆盖）；
  `kvp_curl` 在写入 curl 配置文件前转义 `\` 和 `"`，确保任何密钥字节
  完整到达服务器（裸引号/反斜杠此前被静默篡改）；check-tp4-api.sh 中
  死代码 `AUTH=()` 已删除；env.sh 文档注释干净拆分。
* compress_serde 解压大小上限（延期项 1）：在调用编解码器之前，`plain_len`
  与目标缓冲区和可配置上限（`KVP_DECOMPRESS_MAX_BYTES`，默认 1 GiB）
  比对；zstd 传入 `max_output_size`，lz4 使用流式 `max_length` 界限。
  虚假的 plain_len 或解压炸弹现在产生干净的 ValueError（缓存 MISS），
  不会无界分配。编解码器错误包装为 ValueError（缓存 MISS 契约）。
  6 个新测试，包括真实 zstd 炸弹。
* 启动器对等测试（延期项 2，步骤 1）：`tests/test_launcher_parity.py`
  在 docker 桩上运行两个引擎启动器并比较最终的 `docker run`——能力、
  compilation-config、/w 挂载、/live 挂载和 --env-file 传递的密钥必须
  一致。它发现（本次提交修复）了一个隐蔽分歧：KV 启动器的就绪循环是
  硬编码的（`seq 1 300`、`sleep 10`），忽略了 recipe 启动器遵循的
  `WAIT_TRIES`/`WAIT_SLEEP`——现在两者都遵循。
* 审阅者的 repro-round65.sh 已转化为 `tests/test_manage_check_http.py`
  （场景 A–F：带/不带密钥的 check-tp4-api、正确/错误密钥的 status、
  服务器宕机时 exit 0、无认证模式、特殊字符密钥）。

## 第 65 轮（kvp_curl：密钥走管道——无文件、无 ps、无 unbound 变量）

* 第 64 轮的 `.curl-header` 文件方案导致了三个回归——按审阅者的建议全部
  修复：不再有任何密钥文件。小小的 `kvp_curl()` 通过 stdin 管道发送 bearer
  密钥（`printf | curl -K -`；printf 是 bash 内建命令，在 `ps` 中不可见），
  未设置密钥时执行普通 `curl`（已文档化的 ALLOW_NO_AUTH 回环模式）。
* manage.sh / check-tp4-api.sh 不再因 "LIVE_DIR: unbound variable" 崩溃
  （它们从不定义 LIVE_DIR，而第 64 轮的 `-K "$LIVE_DIR/..."` 在 `set -u` 下
  引用了它）。
* uva 的无认证模式恢复工作：没有密钥时旧代码调用 `curl -K <缺失文件>`
  （退出码 26），就绪循环永远无法成功。`kvp_curl` 在无密钥时不发送头部。
* manage.sh `status` 在服务器停止时不再在 `set -e` 下以退出码 7 终止
  （长期存在的 bug：`code="$(curl ...)"` 在 "not reachable" 分支之前就杀死了
  脚本）。赋值已加保护。
* test-recovery.sh：mktemp 的 header 文件通过 `trap ... EXIT` 删除（/tmp 中
  不留密钥材料），共享的规范密钥（`~/.config/kvp/api_key`）现在优先于
  过时的 `$PWD/live/api_key`。
* kv-persistence env.sh 增加了相同的 `kvp_curl()` 辅助函数；引擎启动器在
  就绪循环中使用它（任何地方都不再有 `.curl-header` 文件）。
* 测试已更新为 kvp_curl 契约。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
