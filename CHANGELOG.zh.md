# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **LMCache 0.5.6**（`requirements-lmcache.txt`）。


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

## 第 63 轮（解析器一致性、密钥不进 ps、/live 挂载、kvp CLI）

* 引擎启动器中的 `_tp4_seed` 现在与 recipe 的解析器逐字节一致：`$HOME`/
  `${HOME}` 被展开，外围引号被去掉，行内注释只在 " #" 处截断，没有有效
  标识符的行（带缩进的注释）被跳过而不是以 "invalid variable name" 崩溃。
* API 密钥不再出现在 `ps` 输出中（全部 5 个 recipe 启动器、KV 启动器和
  test-recovery.sh）：通过 `docker --env-file`（chmod 600）和 `curl -K`
  配置文件传递，而不是 `-e VLLM_API_KEY=<value>` 和
  `-H "Authorization: Bearer ..."` 命令行参数。
* KV 启动器挂载 `/live`（`$ROOT/live:/live`）以便路由计数器持久化；激活
  检查现在也会在 "live snapshot failed" 时失败，并在启用 NUMA 绑定时验证
  `HOOK STATUS numa_bind=ACTIVE`。
* 密钥迁移现在真正写入规范文件 `~/.config/kvp/api_key`（`_kvp_load_api_key`
  中的 "migrate below" 承诺从未实现——两个仓库一直在用不同的密钥）。
* tp4.env 中的 `USE_LOSSLESS_SERDE`/`LMCACHE_L2_SERDE` 现在能到达统一的
  编解码决策（start-all 在解析之前先 seed tp4.env），用户提供的
  `VLLM_EXTRA_ARGS` 不再悄悄移除 MiMo 解析器（关闭：`KVP_MIMO_PARSERS=0`）。
* kv-persistence 中添加了 `KVP_COMPAT_VERSION` 钉定文件（recipe 启动器
  已经在检查它）；`KVP_REPO`/`KVP_REPO_ROOT` 两种拼写都被接受。
* PATH 上的 `kvp` CLI（`scripts/install-kvp.sh` -> `~/.local/bin/kvp`）：
  `kvp up|down|status|logs`，仓库位置记忆在 `~/.config/kvp/`。
* 修正过时文档（README EN/RU/ZH + lmcache-server.env 注释）：省略编解码器
  变量得到无损 zstd，raw 需要显式 `LMCACHE_L2_SERDE=none`。
* 测试：recipe 启动器测试现在遵循 env-file 契约（密钥不应是命令行参数）
  并考虑 `~/recipes` 查找布局。

> 较旧的轮次记录已于 2026-11-07 精简：完整修复历史见 `main` 分支的 git 日志。
