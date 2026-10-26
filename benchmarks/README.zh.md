> 🌐 本页其他语言版本：[English](README.md) | [Русский](README.ru.md) | **中文**

# 基准结果模板

每次硬件运行将 JSON 行写入 `results/<UTC-ts>/bench.jsonl`
（已 gitignore；审阅后请将摘要写入 docs/benchmarks.md）。

模板字段：ts、scenario、prompt_tokens、cached_tokens、ttft_s、total_s、
cache_bytes_before、cache_bytes_after、retrieved_lines、notes。

切勿提交包含对话数据的原始结果文件。