> 🌐 Языковые версии: [English](README.md) | **Русский** | [中文](README.zh.md)

# Шаблон результатов бенчмарков

Каждый аппаратный прогон записывает JSON-строки в `results/<UTC-ts>/bench.jsonl`
(в .gitignore; после проверки перенесите сводки в docs/benchmarks.md).

Колонки шаблона: ts, scenario, prompt_tokens, cached_tokens, ttft_s, total_s,
cache_bytes_before, cache_bytes_after, retrieved_lines, notes.

Никогда не коммитите файлы результатов, содержащие данные переписок.