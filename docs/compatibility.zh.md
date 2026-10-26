> 🌐 本页其他语言版本：[English](compatibility.md) | [Русский](compatibility.ru.md) | **中文**

# 兼容性矩阵

状态标签：**VERIFIED**（已在实际固定的运行时上验证），**NEEDS TESTING**
（有记录但尚未在目标主机上执行），**BLOCKED**。此处不会把未测试的内容说成可用。

## 运行时组合

| 项 | 值 | 状态 |
|---|---|---|
| vLLM 构建 | `vllm/vllm-openai:nightly-8e2beaa3bff…` pinned `@sha256:167ccd86…` | VERIFIED |
| vLLM 提交 | `8e2beaa3bff595577a069d49d96bde9074294ec2`（2026-10-21） | VERIFIED |
| Python | 3.12 | VERIFIED |
| 镜像中的 `LMCacheMPConnector` | 支持 `kv_connector_module_path` | VERIFIED（据源码） |
| LMCache | **0.5.3**，固定于 `requirements-lmcache.txt` | 目标主机 NEEDS TESTING |
| MP 协议兼容（lmcache ↔ vLLM） | ZMQ 请求通道 | 目标主机 NEEDS TESTING |
| L2 `nixl_store_dynamic` 持久化 | persist_enabled + 惰性索引重建 | 目标主机 NEEDS TESTING |
| vLLM 重启后恢复 | 验收要求 | 目标主机 NEEDS TESTING |
| 双服务重启后恢复 | 验收要求 | 目标主机 NEEDS TESTING |
| 模型 | MiMo-V2.6-Pro-MOPD + 自定义补丁 | VERIFIED（生产配方可运行） |
| KV 类型 | 规格书：fp16；生产现为 bf16 | 配置选择；变更后需重测 |
| NVMe 后端 | POSIX（刻意推迟 GDS） | NEEDS TESTING |

## 版本固定策略

* **vLLM** —— 按镜像摘要（`@sha256:`）固定，由启动脚本强制执行。
* **LMCache** —— 固定在 `requirements-lmcache.txt`；上表标明唯一经检验的
  组合。升级需重跑 `scripts/test-recovery.sh`（成功判据是磁盘恢复，而非
  「能导入」）。
* ABI 不匹配时请从生产镜像运行 `lmcache server`
  （见 docs/troubleshooting.md）。

## 为什么选 LMCache

| 候选 | 结论 |
|---|---|
| `nixl_store_dynamic`（LMCache L2） | 选中：persist/recover + 受限容量 + L2 淘汰 |
| `nixl_store` | 否决：预分配注册表，无重启恢复文档 |
| In-process `LMCacheConnectorV1` | 否决：缓存随 vLLM 进程消亡 |
| 仅内置 vLLM 前缀缓存 | 不足：重启即失 —— 正是要解决的问题 |
| 自定义连接器适配器 | 不需要 —— 固定的 vLLM 已带 MP 连接器 |