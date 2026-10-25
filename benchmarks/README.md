# Benchmark results template

Each hardware run writes JSON lines to `results/<UTC-ts>/bench.jsonl`
(gitignored; paste summaries into docs/benchmarks.md after review).

Template columns: ts, scenario, prompt_tokens, cached_tokens, ttft_s, total_s,
cache_bytes_before, cache_bytes_after, retrieved_lines, notes.

Never commit raw result files containing conversation data.
