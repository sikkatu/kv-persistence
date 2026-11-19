# 自定义 LMCache 构建：补丁、构建、身份、重置、未来迁移

本文档覆盖支撑共享 MP 缓存服务器与每个 vLLM 模型 worker 的自定义 LMCache
部署。英文版见 `lmcache-build.md`；俄文版见 `lmcache-build.ru.md`。

## 1. LMCache 构建身份

| 项目 | 值 |
| --- | --- |
| 上游项目 | https://github.com/LMCache/LMCache |
| 固定源码提交（基线） | `07f1c6bb642f4772606a36eab730ae009ddce08f` |
| 基线发布标识 | `0.5.8rc1.post48`（= `git rev-list --count v0.5.8rc1..<pin>` = 48） |
| PR #6551 合并提交（已在基线内） | `61faa902a81462177d40d74f8c64bb9df02163c4` |
| PR #4136 源码修订（补丁） | `b10588bd46288192e01ab06da3c3fa923ad0401a` |
| PR #5972 源码修订（补丁） | `bd79d4c6acf6096061b5092517698239440cb93b` |
| 补丁目录 | `vendor/patches/lmcache/` |
| 补丁细节与校验和 | `vendor/patches/lmcache/SOURCES.md` |
**移植规则（r15）：**取自 PR head 的补丁必须在固定基线上通过静态导入门禁
（`implementation/kvpersist/check_patched_imports.py`，由 `build-lmcache.sh`
在补丁应用后、docker 阶段之前运行）。**「能干净应用」≠「语义上是最新的」**——
上游的移动/重命名（如 `IPCCacheServerKey` 移至
`lmcache.v1.common.custom_types`）能通过 `git apply` 的 fuzz 匹配，却只在
20 分钟构建内的导入阶段失败（硬件日志 19.11 14:57，C11）。每次更换 pin 后
重新运行门禁并把补丁移植到新基线（在补丁树中修正过时名称后用 `git diff`
重新导出——不要手工编辑 hunk）；绝不为了放过过时补丁而削弱/跳过门禁。

可变的 `nightly` 标签从不用于生产；每次构建都检出精确基线提交。
`scripts/build-lmcache.sh` 验证基线修订及 #6551 为其祖先，确定性地应用两个
补丁（补丁无法应用或已被意外应用时响亮失败），运行 #4136 与 #5972 的聚焦
上游回归测试，用 `--no-build-isolation` 针对镜像内已安装的 PyTorch 构建

### 参考构建流水线 C0-C16：步骤 -> 覆盖测试（r11）

| 步骤 | 门禁 | 真实程序测试 |
| --- | --- | --- |
| 补丁导入 | 补丁树静态导入门禁（docker 之前） | test_stale_import_refused_with_message / test_fixed_import_passes / test_fallback_import_not_flagged / test_namespace_package_accepted / test_exempt_modules_and_proto_gen / test_empty_file_list_refused / test_gate_wired_in_build_before_docker（test_r15_patched_import_gate.py）+ test_no_stale_ipccacheserverkey_import_in_patches / test_5972_carries_the_fixed_import / test_sources_md_sha256_matches_patch_files（test_r15_patch_porting.py） |
| C0 | 前导：trap EXIT chown、baseline pip freeze | test_chown_runs_even_on_pytest_failure（test_r8_gpu_gate_and_artifacts.py） |
| C1 | apt update -> git + cuda dev pkgs（响亮拒绝） | test_git_is_installed_before_first_use / test_missing_git_package_refuses_loudly（test_r9_git_install.py，真实 bash+apt 日志） |
| C2 | git safe.directory 在安装之后 | 同 r9 测试（顺序由 apt/git 日志断言） |
| C3 | nvcc + g++ + ninja 门禁、架构覆盖 | test_project_arch_override_wins_over_image（test_r10_build_order.py） |
| C4 | 带引擎 constraints 的构建依赖，无静默回退 | test_engine_freeze_diff_refuses_changed_torch（同一 constraints 逻辑） |
| C5/C16 | 恰好一次 `pip wheel` | test_exactly_one_pip_wheel_in_the_script + test_pip_wheel_runs_exactly_once（真实运行、记录型 pip shim） |
| C6a | wheel 文件清单对比 git ls-files | 真实运行夹具覆盖（缺文件 = 拒绝） |
| C6b | 两个原生扩展 | test_wheel_without_lmcache_native_refused（test_r10_build_order.py）+ test_check_wheel_native.py（真实 zipfile+CLI）+ test_c6b_identity_provenance_chain_delivers_valid_json（r12：真实 check_wheel_native -> C6b -> C15 -> provenance，json.loads） |
| C6c | cuobjdump sm_120，无静默失败 | test_c6c_works_without_unzip / test_c6c_empty_cuobjdump_refuses_loudly / test_c6c_sm89_only_refuses_loudly（r12，对提取出的 C6c 块运行真实 bash）+ test_every_refusal_scenario_prints_a_message（验收遍历）+ test_r10_build_order.py 夹具的架构拒绝路径 |
| C7 | wheel 安装 + pip show 前后（SIGPIPE 安全） | test_c7_reaches_after_marker_despite_big_pip_show / test_c7_before_line_no_false_not_installed / test_c7_before_line_reports_missing_install（r13，真实 bash，200 KB pip 桩）+ test_no_head_pipeline_outside_fail_soft_substitutions（lint） |
| C8 | 从已安装分发位置 import-check（支持 dist-packages） | test_layout_run_sees_installed_not_source / test_plugin_refuses_source_tree_with_exit4（真实 pytest + 插件）+ test_installed_dist_packages_layout_passes / test_source_tree_refused / test_metadata_and_module_mismatch_refused / test_plugin_passes_dist_packages_layout（r14：共享 assert_installed，两种布局） |
| C14 | engine freeze diff（在测试之后、C15 之前；KVP_BUILD_SKIP_TESTS=1 时也执行） | test_engine_freeze_diff_refuses_changed_torch（真实 python 处理 freeze 文件）+ test_c14_runs_after_pytest_install_and_detects_mutation / test_c14_runs_even_when_tests_are_skipped / test_c14_positive_control_no_mutation（r12，真实完整脚本，带状态的 pip shim） |
| C9 | 最小测试依赖（仅 pytest） | 真实运行夹具的接线 |
| C10 | staging 不含 lmcache/ | test_layout_run_sees_installed_not_source（staging 拷贝被断言） |
| C11 | staged pytest + kvp_assert_installed（传入 KVP_LMC_SRC，source-tree 防护生效） | test_script_runs_the_exact_command（每个调用都须 staged 形式）+ 插件测试 + test_distribution_inside_source_roots_refused / test_plugin_refuses_distribution_inside_source_roots（r15 P2.2） |
| C12 | 摘要门禁（skipped==0，passed>0） | test_skip_in_five_cpu_files_still_aborts（test_r8_gpu_gate_and_artifacts.py） |
| C13 | GPU 步骤（skip != 0 => 拒绝） | test_gpu_step_skip_is_not_success（test_r9_gpu_step_honesty.py） |
| C15 | identity.env 单引号、严格 source | test_identity_env_sources_cleanly_under_strict_bash + test_provenance_json_fields（真实 bash/json）+ test_c6b_identity_provenance_chain_delivers_valid_json（r12 端到端） |
| 主机 | provenance 字段取自 identity.env | test_provenance_json_fields（真实运行 build-lmcache.sh）+ test_c6b_identity_provenance_chain_delivers_valid_json（r12：native_extensions 的 json.loads） |

构建步骤顺序（r10）：apt/git → 构建依赖（含 proto 桩生成器所需的
`grpcio-tools==1.78.0`）→ `pip wheel` → wheel 内容检查（git ls-files +
两个原生扩展）→ `pip install --no-deps --force-reinstall` → 从
site-packages 做 import-check（`python3 -P`，三条 `__file__` 证明行）→
pytest（五个 CPU 文件，skipped == 0）→ identity/provenance。架构列表取自
`KVP_TORCH_CUDA_ARCH_LIST`（默认 12.0；镜像值仅为回退），且构建出的
`cuda_ops*.so` 必须含 sm_120（cuobjdump）。`provenance.json` 从
identity.env 读取 `torch_cuda_arch_list`、`cuda_ops_archs` 和
`native_extensions`——它反映真实构建内容。

### 构建期回归门禁与 GPU 回归（r8 P0）

构建容器不带 `--gpus` 运行，因此强制回归门禁仅限五个 CPU 测试文件
（要求 0 skipped）：`test_lmcache_mp_connector`、`test_vllm_mp_adapter`、
`test_lmcache_driven_transfer_skip`、`test_native_state_lookup`、
`test_l1_owner_completion`。构建期**不**检查的部分：
`tests/v1/multiprocess/test_cache_server.py` —— GPU 集成套件（LMCache
服务器 + CUDA IPC），无 CUDA 时在模块级 skip。仅当 `KVP_BUILD_GPU_TESTS=1`
时作为单独步骤运行（此时容器需以 `--gpus all` 启动）；`provenance.json`
记录 `gpu_regression_run: true|false`。构建期的 GPU 覆盖来自
`scripts/build-image.sh` 的 GPU-smoke（在构建镜像上每卡运行一个原生内核）。

wheel，并将完整源码身份与 wheel SHA-256 写入
`live/lmcache-build/provenance.json`。

`provenance.json` 中的 `tests_skipped` 表示整个测试阶段通过
`KVP_BUILD_SKIP_TESTS=1` **整体**跳过——它不是关于单个 pytest skip
（那些会被门禁拒绝）。`gpu_regression_run` 是布尔记录，表明 GPU 回归
步骤是否真正运行并通过（模块级 skip 不算运行）。

两个补丁都小、不共享任何文件、可独立移除：

* `4136-mp-retrieve-abort.patch` — 仅 vLLM MP 集成层
  （显式 `LOADING` retrieve 生命周期、调度器确认的中止传播、无效 store
  完成抑制、中止 ID 去重）；
* `5972-incomplete-kv-objects.patch` — 仅 MP 缓存引擎层
  （窗口下采样后检查块可用性；保留几何中仍含 `null_block_id` 的不完整对象
  在持久化前被拒绝；独立完整的组仍被存储）。

## 2. 部署

`scripts/build-image.sh` 将最终镜像身份写入 `live/image-identity.env`，并把
它烙入镜像 `/etc/kvp-lmcache-identity.json`（基线提交、补丁 SHA-256、
wheel SHA-256）——仅凭 wheel 版本号（`0.5.8rc1.post48`）无法区分我们的构建与
上游 nightly（`no-local-version`）。

**部署状态（r2 P0-1 — 已上线）：** 缓存服务器现在默认为
`KVP_LAUNCH_MODE=docker`，且两个 recipe 启动器在镜像缺少自定义 LMCache
身份时拒**绝启动**（`verify-image-custom.sh` /
`kvpersist.check_image_identity` 在加载模型前比对 `org.kvp.lmc-*` 标签与
`vendor/patches/lmcache/*.patch` 的 SHA-256）。原生 vllm-openai 仅在显式
`KVP_LAUNCH_MODE=host` + `KVP_SKIP_IMAGE_CHECK=1`（测试跳过标志）下启动，
后果由操作者自负。`verify-engine-identity.sh` 还会比对运行中容器的
`/etc/kvp-lmcache-identity.json`（MISMATCH，退出码 4）。

此镜像必须被以下组件使用：

* 共享 MP 缓存服务器（`scripts/start-cache.sh`）；
* 每个 vLLM 模型 worker（recipes 启动器）。

若 worker 与 MP 服务器的 LMCache 构建身份不一致，启动必须失败。
预检身份门禁（`verify-engine-identity.sh`、`_kvp_pre_flights`）在任何模型
加载前执行。

连接器选择：部署的 vLLM 版本要求通过 `--kv-transfer-config` **显式**选择
外部 `LMCacheMPConnector`（不要依赖自动检测）。

## 3. 持久缓存：L1/L2、身份、诊断

* L1 = 主机 DRAM（`LMCACHE_L1_SIZE_GB=64`；请在 `lmcache-server.log` 中
  核实服务器的实际选项——不要假设环境变量被消费）。
* L2 = NVMe 持久层（默认 `KVP_CACHE_ROOT=/var/lib/kv-persistence/l2`；
  必须是本地 NVMe 路径）。
* 编解码器仅限无损：`zstd`（默认）、`lz4`、`none`。
* 缓存身份（namespace、模型标识、KV dtype `bfloat16`、缓存格式、
  object-group 配置、后端标识）在每个 worker 的预检中核验；任何不一致都
  会终止启动。

**`STORE_END` 事件不是持久写入的证据。** STORE/RETRIEVE 诊断区分：
store 已请求 → worker/适配器完成 → 对象被 L2 接受 → L2 写入成功/失败 →
持久化的对象/字节数；retrieve 已请求 → 完成 / 中止 / 从 L2 预取 / 无效对象
被拒绝。实际持久化证据是 `lmcache-server.log` 中的
「L2 store completed」/「L2 prefetch load completed」行。

## 4. 安全的全缓存重置（一次性迁移）

`scripts/reset-persistent-cache.sh` 是**唯一**的破坏性操作，绝不被
start/stop/restart 调用：

```
KVP_RESET_CONFIRM=FULL-RESET bash scripts/reset-persistent-cache.sh
```

要求：显式确认字符串；先停止服务（或 `KVP_RESET_FORCE_STOP=1`）；每个目标
都解析到已配置的缓存根内；拒绝 `/`、home 目录、模型目录、仓库、构建产物
和越界符号链接。重置报告（清除路径及字节数、无关数据保全确认）写入
`live/cache-reset-report-<UTC>.txt`。

### 缓存根标记（`.kvp-cache-root-marker`）

`start-cache.sh` 在初始化缓存时会在缓存根目录打上 `.kvp-cache-root-marker`
标记。破坏性的 reset 通过该标记证明目标确实是缓存目录：显式配置的
`KVP_CACHE_ROOT` 只有在带标记，或者其顶层条目全部是已知的
LMCache/kvpersist 产物（`*.data`、`*.tmp`、`*.bin`、`*.bin.tmp.*`、`objects/`、
`kvp-*`、`index.db`、`crash-*`、`lmcache-server.log/.pid`、
`l2-adapter.rendered.json`、`engine-identity.env`、`.kvp-active-writes`，
以及 `l2-adapter.rendered.json` 中配置的 `relative_tmp_dir` 目录名）时
才被接受。任何顶层的外来条目都会导致拒绝，拒绝信息会列出这些条目。

**对于在标记出现之前创建的缓存**（旧部署）：先查看目录内容，再手动打标记：

```
ls -A <KVP_CACHE_ROOT>
touch <KVP_CACHE_ROOT>/.kvp-cache-root-marker
```

只有当 `ls -A` 显示的全部是缓存数据时才这样做。若缓存根目录含有非标准
内容，先移走外来文件（或仅在完全确定是缓存数据时打标记）。

无论有无标记，reset **始终**拒绝：`$HOME` 与任何用户主目录（`/root`、
`/home/<user>`）、仓库检出目录的任意父目录，以及 `.git` 嵌套深度不超过 2
的任意目录。

绝不删除：模型权重、模型配置、应用数据、recipes、源代码、LMCache 构建
产物、诊断日志、NVMe 上的无关文件或 NVMe 挂载点本身。

## 5. 缓存未命中 / 故障诊断

* 持久 round-trip 检查：`scripts/test-recovery.sh`——冷基线写入 L2，仅重启
  引擎从 L1 恢复，完整重启（引擎 + 缓存服务器）必须显示 L2 读取事件和非零
  `cached_tokens`。成功生成不是缓存命中的证据；请核对 cached tokens 与
  L2 事件。
* 不完整对象拒绝：日志中带「invalid object」的 STORE 跳过意味着 #5972
  拒绝了保留几何中含空块的对象；这些对象此后永远不会表现为缓存命中。
* 后端故障：L2 写入失败始终报告为失败——引擎绝不悄悄报告为已持久化。
* `KVP_LOAD_FAILURE_POLICY` 仍为实验性（默认 `fail`）。旧的 C2 保护配对
  警告已过时：retrieve/abort 竞态已在自定义构建内处理（PR #4136 的下游移植），
  不存在可能冲突的本地保护。**状态（诚实）：implemented, unit-tested;
  hardware validation: C0-C8 passed on the operator's host (old base); new
  base 07f1c6bb6 pending.**
  竞态修复、自定义构建和 cuda_ops 原生扩展已在本仓库通过单元测试
  （implemented, unit-tested）；参考构建流水线已在操作员主机上通过
  C0-C8（wheel 已构建，cuda_ops 含 sm_120，四个原生扩展，安装成功，
  import-check 从已安装分发位置 OK），并在 C11 的过时补丁导入处中止
  （现已修复 + 已添加静态导入门禁）——C9-C16 尚待执行：
  参见 5b 节「实施阶段」剩余的工位验证计划（测试 →
  identity/provenance → build-image → 启动引擎和缓存服务器 →
  含中止的并行负载）。

## 6. 未来上游迁移（#4136 与 #5972 合并后）

计划中的后续操作，不属于当前部署。当两个 PR 都已合并入上游：

1. 检查上游合并提交；选择包含 #6551、#4136 与 #5972 的新固定 LMCache
   源码提交或官方发布（在 Git 历史和源码中验证——新发布不自动包含它们）。
2. 删除 `vendor/patches/lmcache/4136-mp-retrieve-abort.patch`。
3. 删除 `vendor/patches/lmcache/5972-incomplete-kv-objects.patch`。
4. 从 `scripts/build-lmcache.sh` 移除其应用条目，从 `SOURCES.md` /
   `provenance.json` 移除其源码修订行。
5. 删除已被上游复制的临时下游补丁专属测试；保留项目级集成测试。
6. 更新本文档与三个 README，不再把 #4136/#5972 描述为下游补丁。
7. 针对同一固定 vLLM 运行时构建新 LMCache；验证 MP 服务器与每个 worker
   报告同一新构建身份。
8. 运行完整回归套件并重复硬件验证（完整重启后的持久 STORE/RETRIEVE、
   取消压力测试、L1 压力下的 LRU 行为），然后才退役当前构建。

在替换构建通过此验证之前，不得删除当前自定义构建或其补丁。迁移完成后，
项目仓库中不得残留任何手动复制的 #4136/#5972 实现。