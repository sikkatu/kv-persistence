# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


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

## 第 64 轮（统一解析器、auth 全面不进 ps、编解码退出恢复）

* 一个 env-文件解析器（scripts/env.sh 中的 `_kvp_seed_env_file`）取代了两个
  分化的 `_tp4_seed` 副本。start-all 中的副本缺少标识符验证，在带缩进的
  注释上崩溃（"  # X=1" -> "invalid variable name"）；两个调用者现在使用
  同一个函数，语义与 recipe 完全一致。start-all 还在加载配置层之前 seed
  tp4.env，因此 tp4.env 中的 `LMCACHE_L2_SERDE=lz4|none` 不再被静默忽略
  （lmcache-server.env 会预先填入 zstd）。
* Bearer 密钥已从所有脚本的 `ps` 输出中移除（第 63 轮的说法不完整）：全部
  5 个配方的 uva 启动器、manage.sh 和 check-tp4-api.sh 现在使用
  `curl -K <chmod-600 配置文件>` 而不是 `-H "Authorization: Bearer ..."`；
  之前写入但未使用的 `.curl-header` 文件现已接入。test-recovery.sh 也读取
  共享的 `~/.config/kvp/api_key`（可在仓库目录外工作），其 header 文件放在
  /tmp 而不是缓存数据目录中。
* 配方启动器中的编解码退出已恢复：`export LMCACHE_L2_SERDE` 现在只在
  LMCACHE_L2_SERDE / KVP_L2_SERDE / USE_LOSSLESS_SERDE 都未设置时发生——
  `USE_LOSSLESS_SERDE=0` 和 `KVP_L2_SERDE=none` 再次到达 identity gate
  （第 63 轮的无条件 export 用 zstd 覆盖了它们）。
* `kvp up` 传递记住的 recipe 位置（`export RECIPE_DIR=`）——自定义克隆位置
  不再以 "production recipe not found" 失败。`kvp` 仅在首次使用或显式指定
  时写入保存的路径（一次性的 `KVP_REPO=/tmp/x kvp status` 不会重写配置），
  安装器使用符号链接而非复制（`git pull` 立即更新命令）。
* hotsplit_env.sh：shebang 恢复为第一行（兼容性钉定注释被加在它上面了）。
* 测试已更新为新契约：编解码默认值让位于退出开关，auth 通过 curl 配置文件。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
