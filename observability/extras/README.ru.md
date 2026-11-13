# KV-Persistence: дополнения к наблюдаемости (Extras)

Каталог содержит **слой extras** мониторинга kv-persistence: дашборд по
метрикам движка vLLM и (опционально) телеметрия GPU. Появился в раундах
ревью 87–88 (ноябрь 2026) как надстройка поверх дашбордов LMCache; базовый
стек работает и без этого каталога целиком.

```
extras/
  README.md (этот файл; README.ru.md / README.zh.md рядом)
  scrape.d/vllm.yml gpu.yml       # scrape-задания Prometheus (file_sd)
  rules.d/vllm.yml gpu.yml        # recording rules -> стабильные имена kvp:*
  expected-metrics.txt            # отпечаток имён vllm:* @ v0.32.1rc1.dev197
  targets/                        # цели file_sd, генерируются под машину (gitignored)
  relay/nginx.conf.template       # релей метрик vLLM (loopback движка -> bridge-сеть)
  gpu/dcgm-counters.csv           # счётчики DCGM по умолчанию (базовые поля)
  gpu/dcgm-counters.prof.csv      # профилирующие поля DCGM (opt-in, SYS_ADMIN)
  grafana/dashboards/kvp-inference.json   # дашборд (uid: kvp-inference)
  check.sh                        # самопроверка: --static / --live
```

## Что куда монтируется

Единственный override-файл (`../docker-compose.override.yml`) добавляет три
сервиса с **compose-профилями** рядом с четырьмя базовыми контейнерами.
Ничего не публикуется на хосте: Prometheus ходит к экспортёрам через
compose-сеть, релей — к движку через loopback хоста.

| сервис | профиль | образ (закреплён) | что делает |
|---|---|---|---|
| `vllm-relay` | `extras` | `nginx:1.29.4-alpine` | проксирует GET /metrics от движка (127.0.0.1) на адрес шлюза Docker bridge; заголовок Authorization удаляется, всё кроме GET /metrics — 404/405. |
| `dcgm-exporter` | `gpu` | `nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-ubuntu22.04` | телеметрия GPU через NVIDIA DCGM. Список счётчиков — `gpu/dcgm-counters.csv` (монтируется в `/etc/dcgm-exporter/default-counters.csv`). Тег проверен по `https://nvcr.io/v2/nvidia/k8s/dcgm-exporter/tags/list` 2026-11-13. |
| `nvidia-smi-exporter` | `gpu-smi` | `utkuozdemir/nvidia_gpu_exporter:1.3.0` | запасной экспортёр через nvidia-smi (`KVP_OBS_GPU_EXPORTER=smi`), без метрик PCIe. |

**Размеры образов не выдуманы** (правило 11.3) — выполните `docker image ls`
после первой загрузки и впишите реальные значения.

## Как включать/выключать

| переменная | по умолчанию | эффект |
|---|---|---|
| `KVP_OBS_EXTRAS` | `1` | `0` = не разворачивать ничего из extras (базовый стек не трогается) |
| `KVP_OBS_GPU` | `auto` | `off` = GPU-экспортёр никогда; `on` = принудительно; `auto` = если есть nvidia-smi + NVIDIA container runtime |
| `KVP_OBS_GPU_EXPORTER` | `dcgm` | `smi` = экспортёр nvidia_gpu_exporter (профиль gpu-smi) |
| `KVP_OBS_GPU_PROF` | `0` | `1` = профилирующие поля DCGM (SM/DRAM active, PCIe RX-TX). Только профиль gpu; добавляет cap_add: SYS_ADMIN |
| `KVP_OBS_RELAY_PORT` | `30008` | порт релея (только адрес шлюза bridge, никогда 0.0.0.0) |

## Особенность первого запуска — однократный перезапуск базовых контейнеров

Extras монтирует **новые тома в `obs_prometheus` и `obs_grafana`**. Compose
ПЕРЕСОЗДАЕТ эти два контейнера при ПЕРВОМ запуске `start-all.sh` с extras.
Тома данных (`prometheus_data`, `grafana_data`) сохраняются — история и
дашборды переживают пересоздание; сами контейнеры перезапускаются один раз.
Последующие запуски ничего не меняют. Это неизбежно при монтировании нового
конфига в уже работающие контейнеры.

## Дашборд: `kv-persistence - Inference (vLLM + GPU)`

Провижинится автоматически в папку Grafana **kv-persistence** (uid
`kvp-inference`), смотреть: Dashboards → kv-persistence. Отвечает сверху вниз:

* сколько чатов считается vs ждёт в очереди (Running / Waiting);
* скорость одного чата (per-stream tok/s) — ключевая цифра;
* заполнение пула KV (KV usage %) — 80% жёлтый, 90% красный, дальше вытеснения;
* ряд GPU: честная нагрузка — **мощность**; NVML «utilization» показывает 100%,
  когда на GPU резидентно ядро, это НЕ загрузка (заголовок панели это говорит).

Дашборд читает ТОЛЬКО recording rules `kvp:*`, сырых `vllm:*`/`DCGM_*` в
выражениях нет. При переименовании метрики в новой версии vLLM или DCGM
правится одна строка в `rules.d/*.yml`, все 32 панели продолжают работать.

### Пустые панели — причины и действия

| панель | вероятная причина | как проверить |
|---|---|---|
| Running / Waiting / весь ряд vLLM | движок не запущен (relay `up=0`) или порт != `PORT` | `bash extras/check.sh --live`, job vllm; лог движка `Starting vLLM server...` |
| KV usage % | запросов ещё нет (0 — норма) | отправить запрос, панель должна сдвинуться |
| весь ряд GPU | `KVP_OBS_GPU=off`, auto не нашёл NVIDIA runtime, экспортёр упал | `KVP_OBS_GPU=on bash scripts/start-observability.sh`; `docker compose logs` |
| значения GPU нулевые/неверные | DCGM не отдаёт поля на этом GPU/драйвере; в правилах or-фолбэки молча пропускают | `check.sh --live`, WARN «metrics DISAPPEARED/NEW» |
| SM active / DRAM active / PCIe | `KVP_OBS_GPU_PROF=0` (по умолчанию) | задать `KVP_OBS_GPU_PROF=1` |
| unidades часов «kHz» | НИКОГДА; частоты уже в Hz (`kvp:gpu_sm_clock_hertz`, исправлено r88 M3) | см. check.sh --static |
| NaN на per-stream tok/s | нормально при нулевом трафике (нет межтокенных интервалов) | пошлать запрос |

## Ограничения и профилирование (`KVP_OBS_GPU_PROF=1`)

Профилирующие поля DCGM собираются через CUPTI и обычно требуют `SYS_ADMIN`:
`KVP_OBS_GPU_PROF=1` переключает счётчики на `dcgm-counters.live.csv` (копия
`dcgm-counters.prof.csv`, в .gitignore) и включает cap_add в override.
Два задокументированных ограничения NVIDIA: профилирование КОНФЛИКТУЕТ с
Nsight/CUPTI-инструментами (не запускайте одновременно); на виртуализированных
узлах профилирование может быть отключено — панель покажет `no data`, не ошибку.

## Как обновлять

1. поменять закреплённый тег в `../docker-compose.override.yml`;
2. `bash scripts/start-all.sh` (повторный запуск идемпотентен);
3. `bash extras/check.sh --live` — разобрать строки WARN/FAIL.

## Как редактировать

**Провижинируемые дашборды и правила перечитываются из файлов при каждом
запуске — правки в UI Grafana ПЕРЕЗАПИШУТСЯ.** Править JSON и `rules.d/*.yml`
в репозитории, перезапускать, затем `check.sh --static`. Экспорт из Grafana
нельзя возвращать в дерево без очистки `id`/`version`/`meta` (check.sh
требует `id: null`).

## Самопроверка

```
bash extras/check.sh --static   # без docker; 0 = чисто, 1 = FAIL, 2 = WARN
bash extras/check.sh --live     # + здоровье таргетов, релей, дрейф метрик, Grafana
bash extras/check.sh --json     # машиночитаемый отчёт
```

Скрипт только читает — он НИКОГДА ничего не меняет в системе.