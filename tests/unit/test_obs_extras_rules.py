"""Part B round 88 (H2.7): promtool `test rules` fixtures for every rule in
extras/rules.d/*.yml -- SYNTHETIC input series with hand-computed expected
numbers (not derived by replaying the formula), so a wrong operator in a rule
expression is caught, not rubber-stamped.

If `promtool` is not on PATH the tests skip loudly with a reason (per spec
10.2: never pretend-pass without a tool). The fixtures here are also a
documented example for the operator stand.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
RULES = KV / "observability" / "extras" / "rules.d"

# Synthetic series + expected results (hand-computed).
# Power: two DCGM series (30 + 100 W) -> max over the single `gpu` label group
# is per-label; we give ONE gpu label set to keep expectations trivial, and
# test GPU normalization via the smi branch too.
VLLM_TEST = """\
rule_files:
  - vllm.yml

evaluation_interval: 30s

tests:
  # ---- running / waiting / kv usage -------------------------------
  - interval: 5s
    input_series:
      - series: 'vllm:num_requests_running{engine="vllm",model="mimo-pro"}'
        values: '3 4 5'
      - series: 'vllm:num_requests_waiting{engine="vllm",model="mimo-pro"}'
        values: '0 1 0'
      - series: 'vllm:kv_cache_usage_perc{engine="vllm",model="mimo-pro"}'
        values: '0.55 0.62 0.7'
    promql_expr_test:
      - expr: kvp:vllm_requests_running
        eval_time: 10s
        exp_samples:
          - labels: '{}'
            value: 4
      - expr: kvp:vllm_requests_waiting
        eval_time: 10s
        exp_samples:
          - labels: '{}'
            value: 1
      - expr: kvp:vllm_kv_usage_ratio
        eval_time: 10s
        exp_samples:
          - labels: '{}'
            value: 0.62

  # ---- generation tokens/s: 50 tokens over 30s window -> 50/30 ≈ 1.666---
  - interval: 30s
    input_series:
      - series: 'vllm:generation_tokens_total{engine="vllm"}'
        values: '0 50 100'
    promql_expr_test:
      - expr: kvp:vllm_generation_tokens_per_second
        eval_time: 60s
        exp_samples:
          - labels: '{}'
            value: 1.6666666666666667
"""

GPU_TEST = """\
rule_files:
  - gpu.yml

evaluation_interval: 15s

tests:
  # DCGM branch only (no nvidia_gpu_exporter series present):
  #  - power: 120 W for gpu="3"
  #  - used FB: 2048 MiB -> bytes = 2048*1024*1024 = 2147483648
  #  - temperature: 55
  - interval: 15s
    input_series:
      - series: 'DCGM_FI_DEV_POWER_USAGE{gpu="3",UUID="x"}'
        values: '120 120'
      - series: 'DCGM_FI_DEV_FB_USED{gpu="3",UUID="x"}'
        values: '2048 2048'
      - series: 'DCGM_FI_DEV_FB_FREE{gpu="3",UUID="x"}'
        values: '1024 1024'
      - series: 'DCGM_FI_DEV_GPU_TEMP{gpu="3",UUID="x"}'
        values: '55 55'
      - series: 'DCGM_FI_DEV_GPU_UTIL{gpu="3",UUID="x"}'
        values: '88 88'
    promql_expr_test:
      - expr: kvp:gpu_power_watts
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="3"}'
            value: 120
      - expr: kvp:gpu_memory_used_bytes
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="3"}'
            value: 2147483648
      - expr: kvp:gpu_memory_total_bytes
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="3"}'
            value: 3221225472
      - expr: kvp:gpu_temperature_celsius
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="3"}'
            value: 55
      - expr: kvp:gpu_util_percent
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="3"}'
            value: 88

  # smi branch (DCGM absent): ONLY the nvidia_gpu_exporter series exist; the
  # numeric index must be joined from nvidia_smi_gpu_info (H2.5). Two GPUs
  # with uuid A->index 0 and uuid B->index 1 exercise the join, not just
  # label copying: 10 W / 20 W -> two separate `gpu` rows.
  - interval: 15s
    input_series:
      - series: 'nvidia_smi_gpu_info{uuid="A",index="0"}'
        values: '1 1'
      - series: 'nvidia_smi_gpu_info{uuid="B",index="1"}'
        values: '1 1'
      - series: 'nvidia_smi_power_draw_watts{uuid="A"}'
        values: '10 10'
      - series: 'nvidia_smi_power_draw_watts{uuid="B"}'
        values: '20 20'
      - series: 'nvidia_smi_utilization_gpu_ratio{uuid="A"}'
        values: '0.5 0.5'
    promql_expr_test:
      - expr: kvp:gpu_power_watts
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="0"}'
            value: 10
          - labels: '{gpu="1"}'
            value: 20
      - expr: kvp:gpu_util_percent
        eval_time: 15s
        exp_samples:
          - labels: '{gpu="0"}'
            value: 50
"""


@pytest.fixture(scope="module")
def promtool():
    pt = shutil.which("promtool")
    if not pt:
        pytest.skip("SKIP: promtool not installed -- rule arithmetic verified "
                    "on the operator stand only (spec 10.2)")
    return pt


def _write(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body)
    return f


def test_vllm_rules_arithmetic(promtool, tmp_path):
    shutil.copy(RULES / "vllm.yml", tmp_path / "vllm.yml")
    f = _write(tmp_path, "vllm_test.yml", VLLM_TEST)
    r = subprocess.run([promtool, "test", "rules", str(f)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"vllm rule arithmetic failed:\n{r.stdout}\n{r.stderr}"


def test_gpu_rules_arithmetic(promtool, tmp_path):
    shutil.copy(RULES / "gpu.yml", tmp_path / "gpu.yml")
    f = _write(tmp_path, "gpu_test.yml", GPU_TEST)
    r = subprocess.run([promtool, "test", "rules", str(f)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"gpu rule arithmetic failed:\n{r.stdout}\n{r.stderr}"


def test_gpu_rules_promql_syntax_only(promtool, tmp_path):
    """Cheap syntax smoke even when arithmetic expectations drift later:
    `promtool check rules` must accept the real file unmodified."""
    r = subprocess.run([promtool, "check", "rules", str(RULES / "gpu.yml")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"gpu.yml rejected by promtool:\n{r.stdout}\n{r.stderr}"