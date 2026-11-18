#!/usr/bin/env python3
"""r8 docs: GPU regression split documented x3 + CHANGELOG round-8 x3."""
import pathlib

DOC_EN = """
### Build-time regression gate vs GPU regression (r8 P0)

The build container runs WITHOUT `--gpus`, so the mandatory regression gate
is limited to the FIVE CPU test files (0 skipped required):
`test_lmcache_mp_connector`, `test_vllm_mp_adapter`,
`test_lmcache_driven_transfer_skip`, `test_native_state_lookup`,
`test_l1_owner_completion`. What is NOT checked at build time:
`tests/v1/multiprocess/test_cache_server.py` -- it is a GPU-integration suite
(LMCache server + CUDA IPC) that skips at module level without CUDA. It runs
as a separate step when `KVP_BUILD_GPU_TESTS=1` (the container must then be
launched with `--gpus all`); `provenance.json` records
`gpu_regression_run: true|false`. GPU coverage at build time is the GPU-smoke
in `scripts/build-image.sh` (one native kernel per GPU on the built image).
"""

DOC_RU = """
### Регрессионный гейт сборки против GPU-регрессии (r8 P0)

Сборочный контейнер запускается БЕЗ `--gpus`, поэтому обязательный
регрессионный гейт ограничен ПЯТЬЮ CPU-файлами (требуется 0 skipped):
`test_lmcache_mp_connector`, `test_vllm_mp_adapter`,
`test_lmcache_driven_transfer_skip`, `test_native_state_lookup`,
`test_l1_owner_completion`. Что НЕ проверяется на этапе сборки:
`tests/v1/multiprocess/test_cache_server.py` -- GPU-интеграционный набор
(сервер LMCache + CUDA IPC), пропускающийся на уровне модуля без CUDA. Он
запускается отдельным шагом при `KVP_BUILD_GPU_TESTS=1` (контейнер тогда
нужно стартовать с `--gpus all`); `provenance.json` пишет
`gpu_regression_run: true|false`. GPU-покрытие на этапе сборки -- GPU-smoke
в `scripts/build-image.sh` (одно нативное ядро на каждую карту собранного
образа).
"""

DOC_ZH = """
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
"""


def insert_after(path, anchor, block):
    p = pathlib.Path(path)
    t = p.read_text(encoding="utf-8")
    i = t.index(anchor) + len(anchor)
    p.write_text(t[:i] + block + t[i:], encoding="utf-8")
    print(path, "doc inserted")


insert_after("docs/lmcache-build.md",
             "focused upstream regression suites for #4136 and #5972, builds the wheel with",
             DOC_EN)
insert_after("docs/lmcache-build.ru.md",
             "фокусированные регрессионные наборы", DOC_RU)
insert_after("docs/lmcache-build.zh.md",
             "运行 #4136 和 #5972 的", DOC_ZH)

# ---- CHANGELOG x3
CH_EN = """### Round 8 review fixes (P0 + P2, one commit per item -- behavioral tests first)
* **P0** `03029c2`: the GPU-integration suite `test_cache_server.py` (module
  skip without CUDA) no longer gates the GPU-less build. Gate = FIVE CPU
  files (skipped==0); the GPU suite runs only with `KVP_BUILD_GPU_TESTS=1`
  (--gpus all); `provenance.json` records `gpu_regression_run`; gate failure
  lists the SKIPPED rows from `-rs`. Checked the five files: no skipif /
  pytest.skip; only `importorskip` in test_vllm_mp_adapter (importable in the
  vLLM image). Mutation (suite back in the gate) red.
* **P2-1** `f78b7d0`: root-owned build artifacts -- `trap EXIT` chowns
  /usr/src/lmcache /kvp-wheels /kvp-work back to KVP_HOST_UID:GID (also on
  failure); build-lmcache.sh forwards the host uid/gid. Mutation (trap
  removed) red.
* **P2-2** `29d51cd`: GPU_CC in identity.env is shell-safe (`12.0`, not the
  tuple `(12, 0)` that bash parses as an array). Mutation (tuple back) red.

"""

CH_RU = """### Правки ревью раунда 8 (P0 + P2, по коммиту на пункт; поведенческие тесты вперёд)
* **P0** `03029c2`: GPU-интеграционный набор `test_cache_server.py` (модульный skip без CUDA) больше не блокирует сборку без GPU. Гейт = ПЯТЬ CPU-файлов (skipped==0); GPU-набор запускается только при `KVP_BUILD_GPU_TESTS=1` (--gpus all); `provenance.json` пишет `gpu_regression_run`; отказ гейта перечисляет SKIPPED-строки из `-rs`. Пять файлов проверены: нет skipif/pytest.skip; только `importorskip` в test_vllm_mp_adapter (импортируется в образе vLLM). Мутация (набор обратно в гейт) красная.
* **P2-1** `f78b7d0`: артефакты сборки принадлежали root -- `trap EXIT` возвращает владельца /usr/src/lmcache /kvp-wheels /kvp-work на KVP_HOST_UID:GID (также при ошибке); build-lmcache.sh передаёт host uid/gid. Мутация (убрать trap) красная.
* **P2-2** `29d51cd`: GPU_CC в identity.env совместим с shell (`12.0`, а не кортеж `(12, 0)`, который bash читает как массив). Мутация (кортеж обратно) красная.

"""

CH_ZH = """### 第 8 轮审阅修复（P0 + P2，一项一提交；行为测试先行）
* **P0** `03029c2`：GPU 集成套件 `test_cache_server.py`（无 CUDA 时模块级 skip）不再阻塞无 GPU 构建。门禁 = 五个 CPU 文件（skipped==0）；GPU 套件仅在 `KVP_BUILD_GPU_TESTS=1`（--gpus all）时运行；`provenance.json` 记录 `gpu_regression_run`；门禁失败列出 `-rs` 的 SKIPPED 行。五个文件已核查：无 skipif/pytest.skip；仅 test_vllm_mp_adapter 有 `importorskip`（在 vLLM 镜像中可导入）。变异（套件回到门禁）为红。
* **P2-1** `f78b7d0`：root 拥有的构建产物——`trap EXIT` 将 /usr/src/lmcache /kvp-wheels /kvp-work 归还 KVP_HOST_UID:GID（失败时也执行）；build-lmcache.sh 传递 host uid/gid。变异（去掉 trap）为红。
* **P2-2** `29d51cd`：identity.env 中的 GPU_CC 对 shell 安全（`12.0`，而不是 bash 解析为数组的元组 `(12, 0)`）。变异（元组回来）为红。

"""

CH_MARKS = {
    "CHANGELOG.md": ("### Round 6-7 review fixes", CH_EN),
    "CHANGELOG.ru.md": ("### Правки ревью раундов 6-7", CH_RU),
    "CHANGELOG.zh.md": ("### 第 6-7 轮审阅修复", CH_ZH),
}
for path, (marker, block) in CH_MARKS.items():
    p = pathlib.Path(path)
    t = p.read_text(encoding="utf-8")
    assert marker in t, marker
    p.write_text(t.replace(marker, block + marker, 1), encoding="utf-8")
    print(path, "changelog inserted")