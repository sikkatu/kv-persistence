# 更新日志

kv-persistence 的所有重要变更，最新的在前。此处仅保留最近 3 轮
（用户要求）——完整修复历史见 `main` 分支的 git 日志。

语言版本: [English](CHANGELOG.md) | [Русский](CHANGELOG.ru.md) | **中文**

## 未发布

运行时：固定 vLLM 镜像（`@sha256:167ccd86…`）+ **自定义 LMCache 构建**
（基线 `0.5.7.dev428` = `03ee5b82418b6bd44e424cdd46932d9346f43eb2`，
针对上游 PR #4136 与 #5972 的下游补丁，见 `docs/lmcache-build.zh.md`）；
原先固定的 **LMCache 0.5.6** 已被此自定义构建取代。
`requirements-lmcache.txt` 现在指向派生镜像。
### 第 23 轮（第 22 轮验收：flake、live/ 污染、socket 类型错误）
**评审（2026 年 11 月 23 日）：** r22 测试不稳定（12 次运行中 4 次红 -- 桩在 argv
写完和 SIGTERM trap 安装之前就应答 ready）；7 个测试文件写入仓库真实 `live/`
（测试通过只因先前测试污染了目录树）；fd-channel 探测使用 SOCK_STREAM，而 pin
的 lmcache 使用 SOCK_SEQPACKET（EPROTOTYPE 被误判为 stale -> smoke 越过活的服务
器继续）；无效 `LMCACHE_EVICTION_POLICY` 仍以空 argv 启动服务器（进程替换吞掉了
rc=1）；3 个存活变异体（B5-4/B5-5/B6-2）。
* **D1** (P1)：所有测试桩使用 `ready` 文件同步 -- `mp_ping` 桩等待启动桩写完 argv
  并安装 SIGTERM trap。50 次 flake 检查全绿。
* **D2** (P1)：`tests/conftest.py` -- 整个会话使用隔离的 `KVP_LIVE_DIR` + 会话
  守卫：运行前后对仓库真实 `live/` 做哈希，若发生变化则测试失败；各 helper
  （`_read_argv_via_stub` / `_run_smoke_with_server_stub` / `_start_cache_argv`）
  拥有自己的 tmp `live/`（image + image-identity.env 写在那里，绝不写仓库的）。
  干净克隆验收运行（无 `live/`）又暴露了两处被污染目录树中旧工件掩盖的洞：
  启动器的契约记录（engine_container、obs-relay/、.engine-env 等）在每个测试后
  恢复；以及 build-image.sh 中【真实的】构建阻塞（它在 mkdir -p 目录之前就写
  live/image-identity.env -- 干净克隆上的构建在 smoke 成功之后失败）。
* **D3** (P2)：fd-channel 探测改用 AF_UNIX SOCK_SEQPACKET（pin 的 lmcache 类型，
  fd_channel.py:14,53,92）并 fail-safe：仅在【确证】stale（FileNotFoundError /
  ConnectionRefusedError）时放行；EPROTOTYPE 及其他任何 OSError -> 拒绝。
  3 个新测试 + 2 个变异体。
* **D4** (P2)：`_kvp_server_argv_read` 自行构建 argv（临时文件 + rc 检查 -- 旧的
  进程替换吞掉 rc=1）并通过 bash nameref 校验 argv[0]=="server"；start-cache.sh
  两个分支和 smoke 在 `LMCACHE_EVICTION_POLICY` 无效时于任何启动【之前】拒绝。
* **D5** (P2)：closed/persist 标记分开测试（B5-4 被捕获）；`KVP_SMOKE_TERM_TIMEOUT`
  （B5-5 被捕获 -- 等待是硬编码的）；build-image.sh 向 smoke 传递
  `${KVP_SMOKE_PORT:-15555}`（B6-2 被捕获 -- 之前传的是生产端口 5555）。
* **D6** (P3)：eviction-policy 大小写不敏感归一化（`${_p,,}`），删除 r22 过时注释，
  提交 `tests/tools/gen_pin_spec.py`（pin fixture 可从 lmcache 源提交复现）。
### 第 22 轮（smoke 运行 pin 接受的命令；操作员 pin；IMAGE 路径）
**硬件日志（2026 年 11 月 21 日）：** `build-image.sh` 在服务器 smoke 上崩溃 ——
`--eviction-policy: invalid choice: 'lru'`（pin 解析器接受 LRU/ARC/IsolatedLRU/noop，大小写敏感）；绕过该问题后适配器的多行 JSON 被拆分为多个 argv 元素（"unrecognized arguments"）——这是 r20 K6.3 的回归，也破坏了生产 `start-cache.sh`；SIGTERM 后的正常关闭退出码 143 被 smoke 判定为异常。
* `139cb13` **B1-B6**（P0/P2）：smoke 运行的服务器命令 = pin 接受的命令。B1：`_kvp_normalize_eviction_policy` ——默认 LRU、规范化（`lru`→LRU、`isolatedlru`→IsolatedLRU）、未知值拒绝并列出变量名和四个合法值。B2：`mp_ping` 先导入 pin 路径（`lmcache.v1.client.transport.factory`），回退旧路径，两者皆无则 exit 3；smoke 在 exit 3 时**立即**停止等待。B3：smoke 加载随附配置（`configs/generic/*.env`，不含 `*.local`）——chunk-size 256、policy、watermark、ratio；`<chunk-gb>` 改名 `<chunk-size>`。B4：argv 以 NUL 分隔、适配器 JSON 压缩为单行、统一读取函数 `_kvp_server_argv_read`（三处副本已合并）。B5：干净退出 = 退出码 0 或 143（128+SIGTERM 对该服务器是**正常**的）+ 两个关闭标记。B6：smoke 使用独立端口（15555，`KVP_SMOKE_PORT`），fd-channel socket 被活服务器占用时拒绝。pin 源 07f1c6bb 提取的规格固件 `pin_server_spec.json`（101 个标志，AST 遍历，未手写）。
* `24d61f1` **M1-M4**（P2/P3）：M1——操作员 pin `KVP_LMCACHE_BUILD` 生效：解析器导出 `KVP_RUNNING_LMCACHE_BUILD`（独立名字），pin 在 `identity_from_env_files` 和门禁中保持优先（pin > KVP_RUNNING > read_lmcache_build，始终交叉校验，不匹配 → exit 4 并给出两个构建）；修复真实缺陷（无文件记录的 env pin 被静默丢弃）；golden namespace `kvp-ef44c5ce3016e00c3f450ccdaac0cf9c`（在 7a9e615 上计算）固定在测试中——该提交的缓存保持有效。M2——`IMAGE=sha256:<id>` 和 `IMAGE == live/image` 路径已测试（stock override 拒绝、显式 custom 胜出、损坏的 live/image 拒绝）+ 2 个变异检测器；修复真实顺序缺陷（`== live/image` 分支被 `sha256:*` 吞掉而不可达）。M3——删除死代码标志 `--print-wheel-version`，文档描述真实机制。M4——r21 的"变异"测试（真实代码任何状态下都绿）替换为真实代码检测器；namespace 测试在解析器之后的同一 shell 中计算。
* `d67eab7` **文档**：硬件诊断用的单独 smoke 命令 + "SIGTERM 后 143 是正常的"（`docs/lmcache-build` ×3）。
* 完整运行：报告中的最终计数（kv-persistence 和 recipes，顺序执行，0 failed）。

### 第 21 轮（namespace 绑定到运行镜像、诚实的 K6.4 测试、测试漏洞）
* `775cc64` **L1**（P1，K7 绑定到**运行**镜像）：`check_image_identity.py --print-wheel-version` 是死代码；`cli model-name` 从 `live/image-identity.env`（**最后**一次构建的记录，而非正在启动的镜像）取 `lmcache_build`；门禁仅在操作员设置 pin 时才交叉校验（默认为空）。现在 `verify_custom_image` 返回 `wheel_version`，CLI 打印 `KVP_WHEEL_VERSION=`，`_kvp_require_custom_image` 捕获它并在每条成功解析路径上导出 `KVP_LMCACHE_BUILD`（`KVP_SKIP_IMAGE_CHECK=1` 除外）。`read_lmcache_build` 优先级：env > baked identity JSON > live/ 记录。`verify-engine-identity.sh` **始终**交叉校验（pin 或有效值）与容器的 identity JSON；不匹配 → exit 4 并给出 `lmcache_build` 字段名。测试：场景 A≠B（live/ 记录 A，镜像携带 B）——namespace 跟随 B；无 pin 的门禁拒绝；3 个诚实变异为红。
* `626dcb1` **L2**（P1，K6.4 测试曾意外通过）：r20 桩对 `image inspect` 返回 `{}` —— start-cache.sh 更早在自定义镜像检查处拒绝，codec 探测**从未执行**。现在桩返回真实构建的 labels + identity（deep 检查通过），只有 codec 探测失败。断言：`lossless codec 'zstd' is not available`、`build-image.sh`、无 `MISMATCH`/`NOT the custom`、服务器**从未**启动。变异：中和 mutant start-cache.sh 中的 codec 探测使拒绝**消失**（红色检测器）。
* `cc675d8` **L3**（P2，两个幸存变异已关闭）：**K1d** —— `test_b2_same_digest_other_repo_tag_accepted`（同 base digest 换 repo:tag 接受；不同 digest 拒绝）+ 诚实变异（`check_image_identity.py` 副本恢复字符串比较 → 拒绝同 digest 情况）。**K6c** —— `test_smoke_runs_for_both_codecs_and_markers_gated` 现在是**行为**测试：桩仅为一个 codec 打印 smoke 标记 → 构建拒绝且 `live/image` **不**写入（zstd-only 与 none-only 两种情况）。
* `d98f59c` **L4**（P2，诚实的错误文本）：「LMCache build is unknown」消息给出**真实**动作——通过 `scripts/start-all.sh` 启动（它解析镜像并导出构建），或执行 `bash scripts/build-image.sh`。不再有镜像内 `/etc` 路径，此原因不再提示 `setup-identity.sh`（该提示仅保留给 identity.env pin 错误）。测试：完整 identity.env 无解析器恰好给出此文；门禁的 `lmcache_build` 提示块给出真实动作。
* `0f0248d` **L5**（P2，K7 测试卫生）：`test_gate_crosschecks_lmcache_build` 不再写 `identity.env.local` 到真实 `configs/`——它在 tmp 中的迷你仓库上运行（configs 已复制 + pin 已填充，真实仓库绝不触碰）。`test_mutation_dropping_lmcache_build_from_namespace_is_red` 现在是**诚实**变异：去掉 `lmcache_build` 的 identity 模块副本在哈希载荷中 → 构建 A 和 B 在变异体上塌缩为**同一** namespace。
* `189ce4e` **L6**（P3，IMAGE 中的裸标签）：`IMAGE=kvp-lmcache:latest` 得到清晰拒绝——「IMAGE 必须是 `repo:tag@sha256:<64hex>`（base pin）或 `sha256:<id>`（custom），裸标签**不**接受，请取 `$(cat live/image)`」——而非误导性的「base pin mismatch, rebuild」。正确固定的 stock 仍可解析（K1 路径完好）。测试：行为测试，在**真实** `_kvp_resolve_runtime_image` 上。
* 完整运行：kv-persistence **716 passed, 26 skipped**（全部 skip 为环境因素：promtool、nginx、zstd/lz4、lmcache CLI、live-LMCache、仅硬件）；recipes **448 passed, 5 skipped**（本轮代码未改动）。

### 第 16 轮（4136 测试夹具、junit 门禁、engine-pin 盲区、快速重跑）
* `c354ba9` **P0**（硬件日志 19.11 18:53：新基线上 C0-C10 通过，C11 = 150 passed / 5 failed）：5 个失败全在 `tests/v1/test_lmcache_mp_connector.py`（4136 补丁**新增**的文件）——上游测试针对 8 月的 PR head 编写，用 `__new__` 构造连接器时只设置了当前 `__init__` 的部分属性（`_delay_free_req_ids`、`_can_store`、`_pinned_boundary_state_blocks`、`_lazy_offload_manager`）；请求桩缺少 `num_computed_tokens`；一个测试仍断言已废弃的 `_pending_store` API（生产代码中已不存在）。**仅夹具**（生产未动，逐字节验证）：helper 设置四个属性，桩带 `num_computed_tokens=0` / `[0]` 并加强 `update_num_engine_computed_tokens(0, 32)` 断言，lazy-offload 测试按管理器 API 重写（`on_request_finished` → `sessions_to_end=[]`，`end_session` 不被调用）。补丁从真实补丁树重新导出（git diff，未手工编辑 hunk）；SOURCES.md 记录适配 + 新 SHA-256。硬件上预期 C11：155 passed，0 skipped。**新**静态测试（test_r16_patch4136_fixtures.py）：添加行中含全部夹具属性、无 `_pending_store`、SOURCES.md 携带当前补丁哈希。变异：恢复 `_pending_store` → 2 个测试红。
* `b589b98` **P1**（隐藏缺陷，预先检查）：LMCache 的 pytest.ini 含 `log_cli = true`，真实 pytest 用 `=` 装饰摘要行——旧的 `grep -E '^[0-9]+ (passed|...)'` 在**绿色**运行上匹配不到任何内容 → 健康套件上「pytest summary not found」（r11-r15 的桩打印未装饰的摘要，coder 测试从未看到）。C12/C13 现在通过共享模块从 `--junitxml` 报告计数（tests/failures/errors/skipped 按所有 `<testsuite>` 求和，passed = tests - failures - errors - skipped；rc==0 且 failures==0 且 errors==0 且 skipped==0 且 passed>0；缺失/空/损坏 XML → 明确的 `REGRESSION FAILURE: junit report missing/unparsable`）。文本 grep 已移除；摘要行由脚本自建（`CPU gate PASSED (155 passed, 0 failed, 0 errors, 0 skipped)`）。**新**测试（test_r16_c12_junit_gate.py）在**两种**配置下用真实 pytest（带 LMCache 风格的 log_cli pytest.ini 和不带）：绿色通过、skip 被拒并打印 SKIPPED 行、失败被拒、缺失 XML 被拒，外加静态「无文本 grep」门禁。变异：恢复文本 grep → 5 个测试红。
* `d43576e` **P2**（隐藏缺陷）：`engine_pins` 只匹配 `name==ver` 行——直接引用的 freeze 行（`vllm @ file:///...`）会静默清空两侧，「引擎未变」检查盲目通过。C14 现在解析**两种** freeze 格式（`name==ver` + `name @ url`，名字按 PEP 503 归一化，直接引用按 `ref:<url>` 固定），**要求** baseline freeze 中有 torch 和 vllm（否则 `BUILD REFUSED: engine pins not found in baseline freeze (format not recognised)` + baseline 前 20 行），并打印比较的 pins（`torch==... vllm==...`），而不只是一个数字。**新**测试（test_r16_c14_engine_pins.py，真实 C14 块）：两种格式、无法识别的 baseline 被拒、torch 版本变更被拒并带名字+版本、未变更通过并打印 pins、PEP 503 名字归一化。变异：移除存在性要求 → 红。夹具适配（r11 提取标记 + freeze 夹具中的 vllm 行；r12 pytest 桩写最小有效 junit 报告）。
* `6efc3cd` **P3**：`scripts/rerun-lmcache-gate.sh [WORK_DIR]`——不重建 wheel 的快速 CPU 门禁重跑（约 2 分钟迭代：测试来自源码树，而非 wheel）。重新对钉住基线的 fresh checkout 应用补丁，跑 r15 P1 导入门禁，取已构建的 wheel（参数或最新的带 wheels/*.whl 的 /tmp/kvp-lmc-build.*），在钉住镜像中跑 C8-C12。条件：补丁树的**生产**部分（除 tests/ 和 docs/ 外的一切）必须与构建 wheel 的树匹配（work_dir/rerun-prod-hash.txt 中的哈希 → 不匹配时「Need a full build」）；结尾横幅 `NOT production-acceptable: no provenance/identity written`（不写 live/lmcache-build）；与 _incontainer_build.sh **相同的**门禁逻辑，通过新共享模块 `kvpersist.gate_common`（junit-counts + check-imports），绝不复制。**新**测试（test_r16_rerun_gate.py）用真实 git-diff 补丁在**本地**迷你 git 仓库上端到端跑**真实**脚本（仅 stub docker）：哈希不匹配被拒、缺 wheel 被拒、横幅打印、live/ 无写入、共享模块处处使用。最终验收仍**仅**通过完整 build-lmcache.sh。
* `58fceaf` **P4**：SOURCES.md 修正（5972 还触及 worker_transfer.py / attn_window.py / 5 个文档文件；「20 files together」→ 22 已验证；过时的「rebased manually... docs-only changes were not carried over」措辞已删除——补丁就是精确的 PR-diff）。**新**测试（test_r16_shell_gates.py）从 build-lmcache.sh 提取**真实**的 #6608-祖先门禁和空 GATE_SCOPE 拒绝块，在真实 bash 下运行：非祖先的 PR6608 中止构建，空 scope 被 BUILD REFUSED 拒绝。变异 `if false;` / `true || {` → 红。
硬件验证状态（新措辞，文档 x3）：
「implemented, unit-tested; hardware validation: C0-C10 passed on the operator's host (new base 07f1c6bb6), C11 (4136 test fixtures) fixed in r16, C12-C16 pending」。
两个仓库的全套测试（第 16 轮最终）：
* kv-persistence：620 passed，26 skipped
* recipes：        443 passed，5 skipped
### 第 15 轮补充（P0′：迁移到重新变基的 PR #5972 基线）
* `18e8cde` **P0′**（取代上面第 15 轮的 P0——那个修复保留在 git 历史中作为已记录的**回滚方案**：旧 pin + 旧 5972 + 一行导入修复；历史不重写）。PR #5972 的开发者按我们的请求在 `dev` 上重建了 PR：导入现在是 `lmcache.v1.common.custom_types`，最后一个提交（"Unify retained-block validation across MP store paths"）是实质性新代码（17 个文件，+409/−117）。固定：基线 `07f1c6bb6`（比旧 pin 前进 28 个提交，`dev` #6659），PR head `bd79d4c6` 按精确 SHA（SOURCES.md 中的「head 会移动」规则）。补丁是**精确的** `git diff 07f1c6bb6..bd79d4c6a`（未手工编辑）。在真实克隆上验证：4136 + 5972 在新基线上干净应用（合计 20 个文件），r15 P1 导入门禁报告 0 个问题；新 diff **不能**应用到旧 pin。版本标签 `0.5.8rc1.post48+kvp.<fp12>`（= `git rev-list --count v0.5.8rc1..<pin>` = 48，PEP 440，不低于真实基线）。#6608（`bcafb0314`）获得与 #6551 相同的祖先门禁。**新**测试（test_lmcache_pin_consistency.py）：处处同一个 pin、40-hex、标签跟随 pin 基线、补丁顺序 4136-先于-5972、旧 pin/标签在历史 CHANGELOG 之外消失。变异：在一个文件中恢复旧 SHA → 2 个测试红，已验证。**仪表盘指标复检（按 ТЗ 的数字）：** Grafana 仪表盘的 PromQL 中出现 117 个 `lmcache_*` 指标名（审阅中的「108」无法用有记录的方法复现——精确计数方法从未在仓库中固定，两个数字都如实记录）。`lmcache/` 中的名称字面量：223（旧 pin）对 224（新基线）——删除 0，新增 1（新 NIXL 连接器的 `lmcache_nixl.` 前缀，本来就在 `lmcache/` 打包范围之外）。交叉核对：匹配 80 / 静态方法「缺失」37 ——两基线**完全相同**，**迁移丢失：0**；37 个静态缺失是名称重建方法的局限（组合名称/后缀），不是迁移回归——规范 OTel→Prometheus 映射（METRICS.md，90 个导出名称）在新基线上未变。仪表盘中的 `vllm:*` 系列来自 vLLM 本身，不受 LMCache pin 影响。
硬件验证状态（新措辞，文档 x3）：
「implemented, unit-tested; hardware validation: C0-C8 passed on the operator's host (old base); new base 07f1c6bb6 pending」。
两个仓库的全套测试（第 15 轮补充，最终）：
* kv-persistence：598 passed，26 skipped
* recipes：        443 passed，5 skipped
### 第 15 轮（C11 过时补丁导入 + 静态导入门禁 + r14 遗留项）
* `279f17e` **P0**（硬件日志 19.11 14:57）：C0-C8 通过，随后 C11（测试收集）以 rc=2 死亡——`ImportError: cannot import name 'IPCCacheServerKey' from 'lmcache.v1.multiprocess.custom_types'`。5972 补丁取自 PR head（10 月 27 日）；上游 c4fb19dd 在该 head **之后**把类移到了 `lmcache.v1.common.custom_types`——补丁能干净应用（fuzz）但**语义上已过时**（「能干净应用」≠「是最新的」）。修复：从真实补丁树**重新生成**补丁（应用到 pin、修正导入、用 git diff 重新导出——未手工编辑 hunk）；SOURCES.md 中 Patch SHA-256 已更新；我们自己 `tests/integration/lmc_protocol_client.py` 中同样的过时导入已修复（它在 try/except 下默默禁用了协议客户端）。**新**测试（test_r15_patch_porting.py）：无任何添加（`^+`）行携带过时路径，5972 携带修正后的路径，SOURCES.md 中每个 Patch SHA-256 与实际字节一致。4136 补丁以同样方式审计：下面的门禁对两个补丁报告 0 个问题。
* `5c54612` **P1**：补丁树静态导入门禁——`implementation/kvpersist/check_patched_imports.py`（审阅中的参考实现，纯 AST，无需 torch/vllm）接入 build-lmcache.sh，紧跟补丁/fingerprint 之后、docker 阶段**之前**。范围：补丁触及的文件 ∪ 五个 CPU 门禁测试 ∪ 其传递性 `tests/...` 导入；**空**范围即拒绝。拒绝消息为 `BUILD REFUSED (stale patch/test import): <file>:<line>: ...`。**新**测试（test_r15_patched_import_gate.py，离线、真实 Python、合成迷你树）：(a) 过时导入 → 拒绝 rc=1，(b) 修正导入 → rc=0，(c) try/except ImportError 中的 fallback 不标记，(d) PEP 420 命名空间包通过，(e) `_proto_gen` / `lmcache.lmcache_*` 豁免，(f) 空文件列表 → 拒绝，+ 接线检查（门禁在 docker 之前被**调用**——注释中的提及不算）。在真实 pin 克隆上验证：修正树 0 问题，旧 5972 恰好 1 个问题。夹具（test_docker_stdin_delivery / test_r9_gpu_step_honesty / test_r11_gates_real_programs）已适配门禁。变异：删除门禁调用 → 接线测试红。
* `ca1d449` **P2**（r14 审阅遗留）：C11/C13 向插件传递 `KVP_LMC_SRC="$LMC_SRC"`（source-tree 防护此前比 C8 弱），并补充缺失的防护测试：分发根目录位于 source_roots **内**（editable 类似场景）→ BUILD REFUSED，通过 assert_installed 和真实 pytest 中的插件（exit 4）验证。变异：禁用防护 → 两个测试都红。
诚实限制（按 ТЗ）：门禁捕获过时的导入**路径/名称**，不捕获行为漂移——失败的补丁测试意味着移植**补丁**，绝不取消选择/跳过/xfail。
硬件验证状态（新措辞，文档 x3）：
「implemented, unit-tested; hardware validation: C0-C8 passed on the operator's host; C9-C16 pending」。
两个仓库的全套测试（第 15 轮最终）：
* kv-persistence：593 passed，26 skipped
* recipes：        443 passed，5 skipped
### 第 14 轮（C8 误拒绝：dist-packages 布局 vs site-packages 子串）
* `d15c5fc` **P0**（硬件日志 19.11 12:52）：C0-C7 通过（r13 在硬件上确认），随后 C8 **错误地**拒绝了正确安装的 wheel——`BUILD REFUSED: lmcache imported from /usr/local/lib/python3.12/dist-packages/lmcache/__init__.py, not site-packages`。vLLM 镜像是 Debian/Ubuntu 布局，pip 安装到 dist-packages；配方启动脚本知道这一点，构建脚本不知道（r11 夹具用了 site-packages 路径，所以测试一直是绿的）。修复：判据按**实质**——scripts/kvp_assert_installed.py 中新的共享 `assert_installed(mods, source_roots)` 通过 importlib.metadata 定位已安装 lmcache 分发根目录，要求每个模块文件位于其**下**（任何布局：site-packages、dist-packages、…），且根目录不在源码树内（$KVP_LMC_SRC/cwd）；拒绝消息带两个路径（file、dist root）。C8 导入共享函数（PYTHONPATH=$KVP_SCRIPTS），pytest 插件使用同一个（惰性 `import pytest`，模块在安装测试依赖前也可用）。**新**测试（真实 python3 -P + 真实插件，带 dist-info/METADATA 的假已安装分发）：(a) site-packages 通过，(b) dist-packages 通过（修复前红：子串拒绝——精确复现硬件日志），(c) 源码树 → BUILD REFUSED，(d) 元数据/模块不匹配 → BUILD REFUSED；同样通过插件（exit 4）。r11 布局测试增加 dist-packages 变体。静态门禁：子串判据已清除（grep 干净），C8 使用共享函数。变异：恢复子串检查 → (b) x3 + lint 红，已验证。
硬件验证状态（按 ТЗ 保留）：
「implemented, unit-tested; hardware validation: C0-C7 passed on the operator's host; C8-C16 pending」。
两个仓库的全套测试（第 14 轮最终）：
* kv-persistence：581 passed，26 skipped
* recipes：        443 passed，5 skipped
### 第 13 轮（C7 的 SIGPIPE：`pip show | head -3` = 静默 rc=120）
* `7bda1bc` **P0**（首次硬件运行，19.11 11:42）：构建通过 C0-C7（wheel 已构建，cuda_ops 仅含 sm_120，四个原生扩展，`pip install` 成功），随后在 C8 之前**静默**死亡（`docker run rc=120`）。原因：`pip show lmcache | head -3`——`head` 关闭管道，`pip show` 写入已关闭的 stdout 并以 120 退出（SIGPIPE，`ERROR: Pipe to stdout was broken`），`pipefail` 下脚本无消息终止（该行**没有** `||`）。BEFORE 行还在已安装时错误地打印 `(not installed)`。与 r12 同类缺陷但在 `$(...)` 之外——r12 的审计和消息遍历器漏掉了普通管道。修复：两行都用 `sed -n '1,3p'`（读取全部输出），`(not installed)` 仅在 `pip show` 真正失败时打印。**新**测试（真实 bash、按标记提取的真实 C7 块、python pip 桩写 ≥200 KB 且在 stdout 关闭时退出 120）：块必须到达 C7 后的标记（修复前红：rc=120——精确复现硬件日志），无错误的 `(not installed)`，真正失败时打印它。验收 lint：_incontainer_build.sh 中无未保护的 `| head` 管道。变异：恢复 `| head -3` → 红，已验证。scripts/ + implementation/ 中所有 `| head` / `| grep -q` / `sed q` 管道已审计——每处均为 fail-soft（`|| true`）、受保护或有界的诊断输出。
硬件验证状态（新措辞，文档 x3）：
「implemented, unit-tested; hardware validation: C0-C7 passed on the operator's host; C8-C16 pending」。
两个仓库的全套测试（第 13 轮最终）：
* kv-persistence：570 passed，26 skipped
* recipes：        443 passed，5 skipped
### 第 12 轮（provenance JSON、C6c 静默失败、C14 顺序）
* `125b571` **P0**：brace 默认值字面量（`${VAR:-{}}` 形式）会给**非空** JSON 多写一个 `}`——bash 在第一个右花括号处就结束参数替换，第二个花括号成了字面量。垃圾数据经 identity.env 流入 provenance.json（`native_extensions` 变成非法 JSON），build-image.sh 在已经成功完成的漫长构建之后崩溃。修复：默认值单独一行赋值（`[ -n "$X" ] || X='{}'`）；此类字面量已全部清除（`grep -rn ':-{' scripts implementation` 为空）。**新**端到端测试（真实程序，无手动替换）：真实 `kvpersist.check_wheel_native` 对含 3 个原生 .so 的假 wheel → 按标记从 _incontainer_build.sh 提取的 C6b 块 → C15 块 → identity.env → 从 build-lmcache.sh 提取的 provenance 块 → `json.loads(provenance.json)`；`native_extensions` 必须是含 lmcache/cuda_ops、lmcache/lmcache_native、lmcache/lmcache_redis 的 JSON 对象。变异：恢复旧行 → 红（JSONDecodeError: Extra data）。
* `6bbc68e` **P1**：`set -Eeuo pipefail` 下 C6c 的静默失败。`.so` 查找用 `unzip`（镜像中**没有**——rc=127 静默退出），cuobjdump 管道在空输出时静默死亡。修复：`.so` 用 python3 zipfile 定位并解压（不依赖 unzip），每个替换管道要么 fail-soft（`|| true`），要么以**明确**的 `BUILD REFUSED` 拒绝（cuobjdump 空输出 → `BUILD REFUSED: cuobjdump produced no architectures` + 原始 `cuobjdump --list-elf` 输出）。**新**测试在真实 bash 下运行真实 C6c 块（按标记提取）：(a) 无 unzip 也能工作（原先 rc=127 静默），(b) cuobjdump 空输出 → 明确拒绝，(c) 仅 sm_89 → `does NOT contain sm_120`。验收遍历器：真实完整脚本的 7 个拒绝场景——每个非零退出必须带 `BUILD REFUSED` / `REGRESSION FAILURE` 消息（它还抓到了 `nvcc --version | tail -1` 的静默死亡，同样已修复）。变异：恢复 unzip 版 C6c → (a)-(c) 红。
* `d4cb10a` **P2**：engine freeze diff（C14）移到测试阶段（C9-C13）**之后**、C15 之前——pytest 安装现在被 diff 覆盖，且该门禁在 `KVP_BUILD_SKIP_TESTS=1` 时也执行。**新**测试用带状态的 pip shim 驱动真实完整脚本：pytest 安装时（测试开）或 wheel 安装时（测试关）变异 numpy 必须拒绝 `BUILD REFUSED: engine packages changed`；干净运行通过。变异：C14 移回 C9 之前 → 红。
* `19f1ded`：r8 真实运行夹具适配新 C6c（shim 中的 python3 zipfile 探测必须真正执行）——夹具适配流水线，门禁不变。
docs/lmcache-build.* 中步骤→测试表格已更新（x3）；措辞保持「implemented, unit-tested; hardware validation pending」。
两个仓库的全套测试（第 12 轮最终）：
* kv-persistence：566 passed，26 skipped
* recipes：        443 passed，5 skipped
### 第 11 轮（参考构建流水线 C0-C16）
* `f6c4312`：`_incontainer_build.sh` 按 C0-C16 顺序重建（baseline freeze → apt/git → 工具链门禁 → 带 constraints 的构建依赖 → 恰好一次 `pip wheel` → wheel 检查（文件 + **两个**原生扩展 + cuobjdump sm_120）→ 安装 → 从 site-packages import-check（`python3 -P`）→ engine freeze diff → 最小测试依赖 → staging 无 lmcache/ → 带新插件 `kvp_assert_installed` 的 staged pytest → 摘要门禁 → 可选 GPU 步骤 → 单引号 identity.env）。主机端：identity.env 严格 source，provenance 字段用独立默认行（旧的 ${VAR:-{}} 会多写一个 `}`），转发 KVP_TORCH_CUDA_ARCH_LIST。
* `c77dc6f`：测试夹具适配新阶段（python3 shim 先剥离 `-P`，C5 的 rm 后重建 wheel；工具链桩；staging 夹具；提取器接受 staged pytest 形式；接线测试要求**每个** pytest 调用都是 staged 形式）。
* docs/lmcache-build.* 中含步骤→测试表格；变异（重复 pip wheel / 旧 brace 默认 / 去掉 -P）全部被抓住。

两个仓库的全套测试（第 11 轮最终）：
* kv-persistence：557 passed，26 skipped
* recipes：        443 passed，5 skipped

### 第 10 轮审阅修复（P0 + P1，一项一提交；行为测试先行）
* **P0-1** `425adec`：容器内阶段现在**先构建 wheel**（构建依赖含 grpcio-tools==1.78.0 → `pip wheel` → wheel 检查 → `pip install --no-deps --force-reinstall` → 通过 `python3 -P` 从 site-packages 做 import-check，打印三条 `__file__` 证明 → pytest）。旧顺序在构建**之前**从 PYTHONPATH=$LMC_SRC 导入 lmcache——但原生模块和 gRPC 桩只在构建后存在，因此阶段总是死于 ModuleNotFoundError("lmcache.lmcache_native")。变异（构建前导入 / PYTHONPATH 指向源码树）为红。
* **P0-2** `c0efd97`：wheel 与镜像门禁要求**两个**原生扩展（cuda_ops*.so **和** lmcache_native*.so——缺后者 LMCache 退化为 CLI-only 模式）；可选的 redis/fs 记入 `native_extensions`；严格 gate 新增 KVP_NATIVE_IMPORT=OK 标记。变异（仅 cuda_ops）为红。
* **P1** `425adec`：TORCH_CUDA_ARCH_LIST 取自项目变量 KVP_TORCH_CUDA_ARCH_LIST（镜像值仅为回退，日志点名覆盖）；identity.env 记录 TORCH_CUDA_ARCH_LIST_USED + cuda_ops_archs（cuobjdump --list-elf 必须含 sm_120，否则 BUILD REFUSED）；provenance.json 从 identity.env 读取 torch_cuda_arch_list / cuda_ops_archs / native_extensions——现在反映真实构建内容。变异（旧的镜像回退）为红。
* **测试补充** `d52ba18`：r8/build-image/live-image 的固定装置适配新的阶段顺序。

两个仓库的全套测试（第 10 轮最终）：
* kv-persistence：549 passed，26 skipped
* recipes：        443 passed，5 skipped

### 第 9 轮审阅修复（P0 + P1，一项一提交；行为测试先行）
* **P0** `20fafcf`：vLLM 镜像没有 git——容器内阶段在第一行（`git config`）以 rc=127 崩溃。修复：在强制 `apt-get update` 后立即 `apt-get install git`（响亮拒绝，apt 尾部可见），`git config` safe.directory 行移到安装之后，`pip wheel` 后用 `git ls-files lmcache`（不含 .py 和 .so）核对 wheel 中 `lmcache/` 的内容——没有 git 时 setuptools_scm file-finder 会静默丢失包数据。外部程序审计表在提交信息中。变异（git config 在安装之上）为红。
* **P1-1** `0ad1f2d`：KVP_BUILD_GPU_TESTS=1 现在传递 `--gpus all`（数组参数）并在 docker run 之前预检 nvidia-runtime；GPU 步骤要求 torch.cuda.is_available() 并像 CPU 门禁一样解析 pytest 摘要——模块级 skip **不**等于通过（skipped != 0 或 passed == 0 → REGRESSION FAILURE 并列出 SKIPPED 行）。仅在真正通过后设置 GPU_REGRESSION_RUN=true。变异（移除 skip 检查 / 移除 --gpus）为红。
* **P1-2** `18e0f5d`：provenance.json 记录**布尔** `gpu_regression_run`（r8 提交承诺但未写入的字段）；identity.env 变量重命名为 GPU_REGRESSION_RUN（主机读取的名称）；三语文档澄清 tests_skipped = 通过 KVP_BUILD_SKIP_TESTS=1 整体跳过，而非 pytest 的 skip。变异（移除字段）为红。

### 第 8 轮审阅修复（P0 + P2，一项一提交；行为测试先行）
* **P0** `03029c2`：GPU 集成套件 `test_cache_server.py`（无 CUDA 时模块级 skip）不再阻塞无 GPU 构建。门禁 = 五个 CPU 文件（skipped==0）；GPU 套件仅在 `KVP_BUILD_GPU_TESTS=1`（--gpus all）时运行；`provenance.json` 记录 `gpu_regression_run`；门禁失败列出 `-rs` 的 SKIPPED 行。五个文件已核查：无 skipif/pytest.skip；仅 test_vllm_mp_adapter 有 `importorskip`（在 vLLM 镜像中可导入）。变异（套件回到门禁）为红。
* **P2-1** `f78b7d0`：root 拥有的构建产物——`trap EXIT` 将 /usr/src/lmcache /kvp-wheels /kvp-work 归还 KVP_HOST_UID:GID（失败时也执行）；build-lmcache.sh 传递 host uid/gid。变异（去掉 trap）为红。
* **P2-2** `29d51cd`：identity.env 中的 GPU_CC 对 shell 安全（`12.0`，而不是 bash 解析为数组的元组 `(12, 0)`）。变异（元组回来）为红。

### 第 6-7 轮审阅修复（P0 + P1，一项一提交；行为测试先行）
* **A-1/A-3/A-4** `68f68c9`：容器内构建阶段从 stdin heredoc 迁移到挂载的**文件**（`scripts/_incontainer_build.sh`）。在操作员机器上复现：`docker run` 没有 `-i` 时 `bash -s` 的 stdin 是**空的**，阶段悄悄消失，脚本在空输出中报「no wheel produced」。docker 桩现在与真正的 docker 行为一致（仅 `-i` 时附带 stdin；argv 中的挂载文件才是真正执行的脚本），失败时输出 docker rc + 工作目录 + pytest-report.txt 最后 20 行。其他 stdin-heredoc 位置已审计（build-image.sh 在 A-2 修复；探测用 -c）。
* **A-2** `9223306` + `6ebaa04`：build-image.sh 的 verify 阶段是挂载**文件**（`scripts/_verify_image.py`），并在记录任何内容**之前**严格校验标记：`KVP_CUDA_OPS_IMPORT=OK`、`KVP_PY_IMPORTS=OK`、`KVP_IDENTITY_FILE=OK`、`KVP_GPU_SMOKE_OK_COUNT == KVP_GPU_COUNT > 0`——旧 heredoc 产生空的 verify.txt 和**虚假成功**。
* **B-1** `8937148`：基础 vLLM 镜像只有一个 pin 文件 `vendor/pinned-base-image.env`。干净克隆**无需** `export IMAGE` 即可构建；`KVP_BASE_IMAGE` 覆盖；构建脚本中 `IMAGE`/`KVP_LMC_IMAGE` 被忽略（带警告）；浮动标签被拒绝，拒绝信息指明 pin 文件。
* **B-2** `049a90f`（kv）+ `71dc08c`（recipes）：custom 镜像 ID **仅**在 verify-gate 之后写入 `live/image`；`IMAGE` 为空时 `start-cache.sh`/`start-all.sh` 与 KV_PERSIST=1 的配方启动器自动取用；失败的 smoke 绝不创建或覆盖该记录。
* **B-3** `4db5b3f`：pin 一致性——digest **只**存在于 pin 文件（build-image.sh 头部硬编码已移除）；交叉检查 mimo-v2.6-pro-hotsplit 的 `tp4.env.example`（无 recipes 检出时明确 skip）。
* **B-4** `10df544`：build-lmcache.sh 预检，每条拒绝附一行「怎么办」（无 docker / 守护进程不可达 / 镜像 pull 体积 / 磁盘空间），README EN/RU/ZH 新增「三条命令从零到运行」并区分基础镜像与 custom 镜像。
* **测试补充** `3cac6c9`：静态 wiring 测试跟随 A-1 的文件迁移；快速开始措辞保持「监控先于缓存」的顺序。

两个仓库的全套测试（第 6-7 轮最终）：
* kv-persistence：533 passed，26 skipped
* recipes：        443 passed，5 skipped

### 第 5 轮审阅修复（P1，一项一提交；行为测试先行）
* **P1-1** `df8f096`：reset 拒绝真实的旧缓存目录（allowlist 不认识 LMCache 适配器真正写入的产物）。以 `fs_l2_adapter.py` / `key_codec.py` 为准：`encode_fs_filename` -> 每个 ObjectKey 一个 `<名称>.data`，临时文件回退为 `final.with_suffix('.tmp')` 或进入配置的 `relative_tmp_dir` 子目录。allowlist 新增 `*.data`、`*.tmp`（窄掩码，不用 `*`）以及从缓存自身 `l2-adapter.rendered.json` 读取的 `relative_tmp_dir` 顶层目录名（无键则不添加）。行为测试（test_reset_legacy_cache_allowlist.py）：无标记的旧缓存无需手动 touch 即可重置；出现一个外来文件即拒绝，未知列表恰为该外来文件；未配置的类 tmp 目录仍被视为外来。变异（移除 `*.data`）为红。
* **P1-2** `c5d75f0`：面向运维的缓存根标记文档 x3（EN/RU/ZH，docs/lmcache-build.* 的 reset 小节）：标记的用途与 start-cache.sh 在初始化时创建；旧缓存的精确流程 `ls -A <KVP_CACHE_ROOT>` + `touch <KVP_CACHE_ROOT>/.kvp-cache-root-marker`（与 reset 拒绝提示逐字一致，提示中亦加入 `ls -A`）；无条件拒绝（$HOME / /root / /home/<user>、检出目录的父目录、`.git` 深度 ≤ 2）。措辞保持「implemented, unit-tested; hardware validation pending」。按本轮规则不写静态词面测试。

两个仓库的全套测试（第 5 轮最终）：
* kv-persistence：515 passed，26 skipped
* recipes：        443 passed，5 skipped

### 第 4 轮审阅修复（P0/P1，一项一提交；行为测试先行）
* **P0-1** `0d20ce6`：CUDA dev 包后缀计算错误（`%.*` 对 torch CUDA `13.0` 输出 `13` → 不存在的 `cuda-cudart-dev-13`，构建总是「BUILD REFUSED」）；缺少 `apt-get update`（vLLM 镜像清空了 apt 列表）；apt 错误被 `>/dev/null 2>&1` 吞掉。修复：`scripts/_cuda_dev_suffix.sh`（唯一真值来源，`major-minor`：13.0→13-0 / 13.0.2→13-0 / 12.8→12-8），安装循环前执行 `apt-get update -y`，任何拒绝都打印 apt 输出的最后 20 行。另 P1：pytest 的空 summary 行现在会给出明确的「pytest summary not found」拒绝。行为测试（test_build_lmcache_cuda_dev.py）提取真实的 in-container apt 代码块并用 stubbed apt-get 运行；后缀通过辅助脚本直接调用验证；`%.*` 突变为红。
* **P0-2** `2493925`：reset 安全回归 — r3 P1-2 与 /mnt//home 封锁一起移除了防护，导致显式 `KVP_CACHE_ROOT=$HOME`（cache.env 中的错字）会清空主目录。修复：显式缓存根必须证明其身份 — `.kvp-cache-root-marker`（由 start-cache.sh 在缓存初始化时创建）或者顶层条目全部属于已知的 LMCache/kvpersist 集合（lmcache-server.log/.pid, l2-adapter.rendered.json, engine-identity.env, index.db, crash-*, objects/, kvp-*, *.bin, *.bin.tmp.<uuid>, .kvp-active-writes）。一律拒绝：$HOME（当前用户和任意 /home/<user> / /root）、checkout 的任意父目录、`.git` 深度不超过 2 的任意目录。拒绝会列出未知条目并提示人工检查后打标记。/mnt/<x>/<dir> 带标记或是已知集合仍然接受。行为测试（test_reset_explicit_root_policy.py 中 6 个用例）修复前为红；已知集合检查突变为红。
* **P0-3** `58a1866`：`--deep` 已存在于 check_image_identity.py，但 verify-image-custom.sh 和 `_kvp_require_custom_image` 调用模块时不带 `--deep`，因此实际启动始终只做 LABEL 检查。修复：两个入口默认执行 deep 路径；`KVP_VERIFY_SHALLOW=1` 是测试台唯一的显式退出开关（与 `KVP_SKIP_IMAGE_CHECK` 同类）。deep 现在还会交叉验证已安装的真实版本（`importlib.metadata.version('lmcache')` 对应 identity 的 wheel_version），对不含 `+kvp.` 后缀的 identity 和元数据不可读时均拒绝。行为测试（test_deep_default_wiring.py）用 docker stub 运行真实的 verify-image-custom.sh（labels 正确，`cat` identity 失败 → rc ≠ 0）；`--deep` 突变为红。
* **杂项** `7ae5464`：恢复 `scripts/_cuda_dev_suffix.sh` 的 exec 位（100755）。

### 第 3 轮审阅修复（P0/P1，一项一提交；行为测试先行）
* **P0-1** `5f45986`：build-image.sh **第一次运行**就失败——Dockerfile heredoc 在计算 `$PR4136_SHA`/`$PR5972_SHA`/`$WHEEL_SHA_ID` **之前**就引用了它们（`set -u` 下的 unbound variable）。已修复：三个 sha256 变量立即在 `cp $WHEEL` 之后计算，删除下方重复块。行为测试（tests/unit/test_build_image_runs.py）用 stub docker + tmp 固定装饰真实跑脚本（KVP_PATCH_DIR/KVP_BUILD_DIR/KVP_IDENTITY_OUT —— 不写入仓库的 live/）；变异检测器恢复有缺陷的顺序并断言确切崩溃。
* **P0-2** `b1afd2e`：git dubious-ownership + wheel 版本固定。容器以 root 运行挂载 host-uid 的 /usr/src/lmcache；git 拒绝读取仓库，且 setuptools_scm 未设 fallback_version → wheel 变成 0.0.0 / 0.1.dev。已修复：`pip wheel` 前 `git config --global --add safe.directory /usr/src/lmcache`；`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=0.5.7.dev428+kvp.<PATCHED_STATE 前 12 hex>`；`kvpersist/lmc_identity.py` 新增 `compare_wheel_version()`（单一事实来源：裸 upstream nightly 字符串即使精确匹配也被拒）。provenance + 烙入的 identity 文件记录 wheel_version。dubious-ownership 行为测试在该沙箱 SKIP（chown 在 tmpfs 上静默 no-op）；在操作员站将真实运行并证伪该 bug。
* **P0-3** `390b6a7`：构建容器安装 CUDA dev 头文件。vLLM 运行时镜像含有 nvcc + cuda-cudart，但**不含** cuda-cudart-dev / libcusparse-dev / libcusolver-dev / libcublas-dev（vLLM 自己的 Dockerfile 在其 LMCache 源码构建阶段安装后 purge）。按 `torch.version.cuda`（13.0 → 13-0）派生后缀的 `apt-get install -y --no-install-recommends` 四个包；缺失包 **指名** 后中止构建（无 `|| true` —— 这正是产出无 cuda_ops wheel 的根源）。`export CUDA_HOME=/usr/local/cuda`。派生镜像 Dockerfile 保持无 dev 包（测试断言无 apt-get/-dev 字符串）。
* **P1-1** `d7275a5`：捕获 pytest 退出码（不再 `|| true`），跳过数取自 pytest 摘要行（绝非比对 SKIPPED 明细行）。
* **P1-2** `12ccd8e`：重置路径策略 —— 显式 `KVP_CACHE_ROOT` 胜过 `/mnt`/`/home` 一刀切禁令（真实 NVMe 常挂载于 /mnt），前缀比较严格 `/` 边界（根 `…/l2` 不再吞掉同级 `…/l2-old`），`KVP_LIVE_DIR` 供测试暂存（绝不写入仓库 live/）。
* **P1-3** `a2deeaf`：LABEL 之外的深度镜像验证（LABEL 一行即可伪造）：通过 `docker run --entrypoint cat` 读取烙入的 `/etc/kvp-lmcache-identity.json` 并与 `vendor/patches/lmcache` 交叉验证补丁 sha，再经 `docker run --entrypoint python3` 探测 `find_spec('lmcache.cuda_ops')` —— 任何不符在启动前中止。CLI 标志 `--deep`，由 `verify-image-custom.sh` 串联。
* **P1-4** `docs`：诚实措辞「implemented, unit-tested; hardware validation pending」替代关于竞态修复/自定义构建/原生扩展的未验证「verified/works」陈述（EN + RU + ZH）；新增 5b 节「Implementation stages」记录 6 步硬件验证链 —— 仅在真正跑过后才改为「validated on hardware」。

### 第 2 轮审阅修复（P0/P1，一项一提交）
* **P1-1** `87b1083`：恢复 r1 重写时丢失的 `nixl==1.5.2`/`openai`/
  `zstandard`/`lz4` 固定（缺 nixl 时 L2 适配器拒启、缺 openai 时 CLI 挂掉）；
  pin 测试改到新契约。
* **P1-2** `95fa1a8`：构建身份烙入镜像（`/etc/kvp-lmcache-identity.json`，
  基线 + 补丁 SHA-256 + wheel SHA-256）并由门禁校验（MISMATCH → 退出 4）；
  `PATCHED_STATE` 哈希 diff 内容；`pytest -rs` 且 skip==失败；三语文档如实。
* **P0-2** `03fe204`：缺 `cuda_ops*.so` 的 wheel 被拒（无 nvcc 的静默
  fallback 正是 cudaMemcpy error 1 的根源）：nvcc 预检、
  `BUILD_WITH_CUDA=1 TORCH_CUDA_ARCH_LIST=12.0`、wheel 内 .so 检查、
  每卡真实 GPU 冒烟（`--gpus all`）、服务器日志 fallback 警告。
* **P0-3** `4fdff85`/`1678964`：reset-persistent-cache.sh 重写 —— 诊断
  log/pid/rendered-json/crash-* 保留（归档到 live/），符号链接缓存根真正
  清空（resolved-path 遍历 + 余留复查），专用缓存目录确认
  （resolved-path 比较，符号链接 KVP_CACHE_ROOT 可用），拒绝网络文件系统，
  侦测活的 host-MP（死 pid 忽略），完整 7.4 报告。9 测试。
* **P0-1** `a259929`：启动时的自定义镜像强制 —— 原生 vLLM 镜像被
  start-cache.sh（默认 `KVP_LAUNCH_MODE=docker`）和两个 recipe 启动器在
  KV_PERSIST=1 时拒绝（镜像标签 vs 补丁 SHA-256，模型加载前执行，
  `verify-image-custom.sh`）；接受本地构建镜像的裸 image-ID
  （`sha256:<64hex>`）细粒度 pin。13 测试。

### LMCache 集成：两个必需的上游修复 + 缓存重置
* 创建 `vendor/patches/lmcache/`：`4136-mp-retrieve-abort.patch`（retrieve/
  abort 竞态：显式 `LOADING` 生命周期、调度器确认的中止传播到 worker、
  无效 store 完成抑制、中止 ID 去重）、`5972-incomplete-kv-objects.patch`
  （窗口下采样后块可用性检查；含 `null_block_id` 的不完整对象在持久化前
  被拒绝，独立完整的组仍被存储），以及 `SOURCES.md`（固定基线提交 +
  各 PR 源码修订 + SHA-256 校验和）。已确认 #6551 已合并入基线
  （通过 `git merge-base --is-ancestor` 验证，无本地重复实现）。
* `scripts/build-lmcache.sh`（新增）：在固定 vLLM 镜像内可重现地构建自定义
  LMCache；验证基线修订与 #6551 祖先关系；确定性地应用两个补丁（无法应用
  或已被意外应用时响亮失败）；运行 #4136 与 #5972 的聚焦上游回归测试；用
  `--no-build-isolation` 针对固定 PyTorch 构建 wheel；将完整源码身份 +
  wheel SHA-256 记录到 `live/lmcache-build/provenance.json`。
* `scripts/build-image.sh`（新增）：从固定 vLLM digest 构建派生镜像，仅安装
  自定义 LMCache wheel；逐位保留 vLLM/PyTorch/CUDA/Python；将最终 digest
  与运行时身份写入 `live/image-identity.env`；此单一镜像同时服务共享 MP
  缓存服务器与所有模型 worker。
* `scripts/reset-persistent-cache.sh`（新增）：唯一的破坏性操作（一次性
  迁移）。要求 `KVP_RESET_CONFIRM=FULL-RESET`；拒绝不安全路径（`/`、
  home、模型、仓库、越界符号链接）；强制/验证先停止服务；写入
  `live/cache-reset-report-<UTC>.txt`。绝不被 start/stop/restart 调用。
* `docs/lmcache-build.{md,ru.md,zh.md}`（新增）：LMCache 构建身份、自定义
  镜像部署、L1/L2 + 缓存身份 + STORE/RETRIEVE 诊断（「STORE_END 不是持久
  写入的证据」）、安全缓存重置流程、缓存未命中诊断，以及在上游合并
  PR #4136/#5972 后移除两个下游补丁的明确未来迁移流程。

### 第 80 轮审阅修复（诚实指标、状态漂移、生命周期）
* `ask()` 指标由 `ttft_s` 更名为 `request_latency_s`（非流式探针测量的是完整
  请求延迟，而非首 token 时间——诚实命名）；
* `start-all.sh` 导出唯一规范 `LIVE_DIR=$ROOT/live`（recipe 的 `$PWD/live`
  默认值在其他目录启动时会漂移）；`live/image` 在 digest 校验之后写入；
* `stop-all.sh` 也停止 observability 栈；`stop-engine.sh` 先 graceful
  `docker stop --time 30` 再 `rm -f`；
* recipe 启动器在直接启动时也运行共享预检（KVP_PREFLIGHTS_DONE 防止重复）；
* 文档清理：所有对已删除 kv 启动器的引用已替换，容器名和硬件验收日期
  （2026-11-10）已更正；
* 被弱化的测试恢复为真实检查 + 回归测试（任意目录启动不漂移状态）。

## 第 80b 轮（legacy kv 启动器已删除；所有检查均已保留）
* **`run-vllm-with-persist.sh` 已删除**——`start-all.sh` 现在只有一条引擎
  路径（`KVP_ENGINE=recipe`）。`KVP_ENGINE=kv` 诚实地报错退出（没有任何
  东西丢失：共享检查都在 `env.sh` / `hotsplit_env.sh` 中）。
* **旧启动器拥有的每一项检查都经过审计并保留**——三项预检（TCP 可达、
  L2 编解码器匹配、lmcache 版本匹配）、引擎死亡时的缓存服务诊断、GPU
  预检 + 缓存服务 PID 检查、`engine-identity.env` 写入、以及 namespace
  覆盖 consent 门（第 72 轮）全部迁移到 recipe 启动器 / 共享的
  `_kvp_pre_flights`。它们曾经只存在于被删除的脚本中（第 76 轮的
  缺陷类别）。
* 22 个 kv + 5 个 recipes 测试文件已迁移：文本契约检查读取契约并集
  （`_targets.contract_text()` / `_contract.py`），行为测试驱动 recipe
  启动器。Parity 测试（两个启动器）变为引擎 docker 契约测试（一个）。
* kv 378+20，recipes 388+5——全部绿色。

## 第 80 轮（默认引擎 = recipe，硬件验证已通过）
* **硬件运行于 2026-11-10 完整通过**（`test-recovery.sh`
  `VERDICT: PASS`，exit 0）：recipe 路径上的 same-UID 引擎（1000:1000），
  场景 A（仅重启引擎）memory/L1 命中，`cached_tokens: 2560`，TTFT 2.44 秒
  对比冷启动 11.49 秒；场景 B（完全重启）磁盘恢复，带 L2 读取事件和
  缓存服务进程读取的 411 MB。
* **`KVP_ENGINE=recipe` 现在是默认值**（`start-all.sh`）。旧的 kv 启动器
  仅作为显式 legacy 路径 `KVP_ENGINE=kv` 保留到第 80 轮清理将其删除；
  所有共享检查都在 `env.sh` 中，两条路径都会运行。

## 第 79 轮（recipe 路径 same-UID 回归修复、recovery 测试 fail-fast + 诊断）
* **反复出现的 `register_kv_caches` 300 秒超时的根源已在硬件上确认**（审阅者 +
  驱动日志）：委派的 recipe 路径以 ROOT 运行引擎，而主机上的缓存服务以普通用户
  运行——正是这种 UID 错配导致 `CUDA_ERROR_MAP_FAILED`（"Failed to
  open/create interprocess memory file / remote IPC semaphore pool" = EACCES，
  /dev/shm 中 root 所有的 `cuda.shm.*`）。第 45h/48 轮的 same-UID 契约在委派时
  丢失——与第 76/77/78 轮同类缺陷。
* `run-tp4-hotsplit.sh` 现在**默认**以 `$(id -u):$(id -g)` 运行引擎
  （`KVP_SAME_UID=1`，退出用 `KVP_SAME_UID=0`），HOME / HF / XDG / TRITON
  缓存放在 `/tmp`——与 `run-vllm-with-persist.sh` 完全一致。
* recipe 路径的 API 等待现在按缓存服务日志中的新鲜 `Error handling operation`
  **fail-fast**（打印根因；标记在 `docker run` 之前采集，秒级故障也能捕获），
  而不是干等引擎的 300 秒超时。
* `test-recovery.sh`：`api_ready` 同样 fail-fast，并在容器死亡时打印
  `docker logs`（无需手动取证）；场景 B 在 `docker start` 之前运行 CUDA IPC
  预检（正是 B 倒下的那一步）；服务日志的所有读取都用 `grep -a`（NUL 字节会让
  普通 grep 静默跳过匹配）。
* `start-all.sh` 向共享预检传递 same-UID 标志（预检必须验证引擎实际使用的边界）。
* 回归测试：`tests/unit/test_round79_recovery_failfast.py`（kv，5 个测试）+
  `tests/test_round79_same_uid.py`（recipes，3 个测试）——在旧代码上为红色。
  parity allowlist 已更新：`--user` 和 `/tmp` 缓存挂载现在是共享 token（两条
  路径都 same-UID），不再是单侧差异。



