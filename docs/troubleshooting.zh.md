> 🌐 本页其他语言版本：[English](troubleshooting.md) | [Русский](troubleshooting.ru.md) | **中文**

# 故障排查

先检查三样东西：

```bash
bash scripts/budget-report.sh status        # 占用、命名空间、磁盘
tail -n 100 "$KVP_CACHE_ROOT/lmcache-server.log"   # 缓存服务
docker logs --tail 100 mimo-pro-tp4-kvp     # 引擎（kv-persistence 启动器）
```

## 预检报身份问题

`[!!] KVP_MODEL_REVISION must be a full 40-hex SHA` —— 仅两种格式有效（每部署择一）：
1. **本地文件指纹**（setup-identity.sh 默认写入）：config.json + *.index.json +
   每个 shard **内容**的 SHA-256。不是提交 SHA，但逐字节标识磁盘上的文件。
2. **HF 提交 SHA** —— 仅当有意追踪来源；注意远端 HEAD 未必等于本地文件版本。

两者均写入 `configs/mimo-v2.6-pro/identity.env.local`（已 gitignore），
追踪的 `identity.env` 不变。分支名永远不接受。

`[!!] KVP_CHAT_TEMPLATE_DIGEST must be 64 hex` —— 带
`KVP_MODEL_DIR="$HOME/models/<your-model-dir>"` 运行预检；摘要按
统计算法计算并与配置对照（无需手动复制）。

## lmcache CLI 缺失或 ABI 不兼容

`ERROR: lmcache CLI missing` —— 按部署步骤 3 安装。导入报未定义符号 =
wheel 与镜像 PyTorch/CUDA 栈不符。备用：从生产镜像运行：

```bash
docker run --rm --entrypoint lmcache "$IMAGE" server --help
```

## 缓存服务已启动但 vLLM 连不上

1. 顺序：start-cache.sh 必须在 vLLM 启动器之前。
2. 两进程共用 host 网络 —— 不可单方面迁移请求端口。
3. 配方集成（KV_PERSIST=1）：启动器应打印
   `KV_PERSIST=1: attaching LMCache connector …`；缺失 = 旋钮没传到。

## 重启后没有命中（TTFT 不降）

1. cached_tokens > 0（若为 null 则指标不可用 —— 测试返回 INCONCLUSIVE）。
2. 日志中 L2/磁盘读事件增长（匹配 test-recovery.sh 中的检索模式）。
3. used_bytes 没有重建同样数据（前后对比 status）。
4. 身份未变 —— 重跑 preflight；版本/dtype/模板变化会进入新命名空间。
5. KVP_PERSIST_ENABLED 为 true。

## 磁盘满 / 写入被拒

提高 KVP_CACHE_BUDGET_GB 或显式清理旧命名空间。服务运行期间勿手动删除文件。

## 淘汰太激进/太迟钝

调高 KVP_EVICT_TRIGGER_WATERMARK 或调 KVP_EVICT_RATIO；先用小隔离预算验证。

## 复用很少（都"冷"）

```bash
python3 "$REPO/implementation/prompt_probe.py" --tokenizer "$MODEL_DIR" \
  --request-file a.json --request-file b.json
```

stable_prefix_tokens 少 = 动态值破坏前缀。为缓存截断历史被禁止。

## 损坏文件恢复

损坏可容忍：未命中即重算。手动清理：停止服务 → purge --all --yes → 再启动。