# observability/ — стек мониторинга LMCache

Полный мониторинг сервиса постоянного кэша kv-persistence, скопированный из
официального примера LMCache (`LMCache/LMCache` `examples/observability`,
Apache-2.0) и адаптированный так, что **одна команда** поднимает всё:

```bash
bash scripts/start-observability.sh
# или, что эквивалентно:
cd observability && docker compose up -d
```

## Что запускается

| Сервис | Порт | Назначение |
|---|---|---|
| OTel Collector | 4320 (OTLP gRPC), 8889 (скрейп) | принимает телеметрию от lmcache-сервера, раздаёт в Tempo + Prometheus |
| Prometheus | 9091 | хранение метрик (ретенция 7 дней) |
| Tempo | 3200 | хранение трейсов + TraceQL-метрики |
| Grafana | **3000** | интерфейс дашбордов (анонимный Admin, без формы входа) |

**Все порты привязаны только к `127.0.0.1`** (loopback-контракт из
`docs/security.md`). Grafana работает с анонимным Admin-доступом и отключённой
формой входа — выставление на `0.0.0.0` раздало бы админку на всю сеть. Для
доступа с другого хоста поставьте пред стек аутентифицирующий обратный прокси;
НЕ расширяйте привязки портов.

**Образы закреплены** на точные версии (`otel/opentelemetry-collector-contrib:0.164.0`,
`grafana/tempo:2.7.2`, `prom/prometheus:v3.15.1`, `grafana/grafana:13.2.5`) —
как требует правило проекта «без `:latest`». Обновление — осознанное действие:
поменяйте тег в `docker-compose.yml`, повторите `docker compose up -d` и
перепроверьте дашборды.

## Grafana

Откройте <http://localhost:3000> → **Dashboards**. Два дашборда уже
предустановлены из `grafana/provisioning/dashboards/`:

* **LMCache** — cache hit rate, скорости чтения/записи StorageManager,
  заполнение L1/L2, эвикции и живая панель трейсов.
* **LMCache Coordinator** — здоровье MP-координатора (заполняется только при
  работающем координаторе).

Источники данных (Prometheus, Tempo) настраиваются автоматически из
`grafana/provisioning/datasources/datasources.yml`.

## Подключение сервиса кэша

Стек пассивен, пока lmcache-сервер не начнёт отправлять телеметрию в
коллектор. С раунда 47b метрики включаются АВТОМАТИЧЕСКИ: `start-cache.sh`
обнаруживает коллектор на `127.0.0.1:4320` и начинает отправку — флаг не нужен.
Сначала поднимите стек, затем кэш:

```bash
bash scripts/start-observability.sh   # коллектор + Prometheus + Grafana
bash scripts/start-cache.sh           # метрики идут сами
```

Явное управление (редко нужно):

```bash
USE_OBSERVABILITY=1 bash scripts/start-cache.sh                      # принудительно вкл
KVP_OTLP_ENDPOINT=http://localhost:4320 bash scripts/start-cache.sh  # свой endpoint
KVP_NO_OBSERVABILITY=1 bash scripts/start-cache.sh                   # принудительно выкл
```

`--enable-tracing` добавляется автоматически при включённом OTLP, поэтому
водопады запросов появляются в Tempo (Explore → источник данных Tempo).

## Метрики, за которыми стоит следить

| Панель / семейство метрик | Значение |
|---|---|
| cache hit rate | соотношение hit / miss на запрос — главный сигнал здоровья |
| L1 / L2 usage | заполнение уровней относительно ёмкости (L1 = RAM хоста, L2 = NVMe) |
| store / retrieve throughput | байт/с, записываемые на NVMe и читаемые с него |
| evictions | LRU-эвикции по уровням; растущий темп означает слишком малый бюджет |
| restore (prefetch) time | как быстро перезапущенный сервер наполняет L1 из NVMe |

## Файлы

| Файл | Содержимое |
|---|---|
| `docker-compose.yml` | стек из 4 сервисов (коллектор, tempo, prometheus, grafana) |
| `otel-collector.yml` | OTLP-приёмник → раздача в Tempo + Prometheus |
| `prometheus.yml` | скрейпит Prometheus-endpoint коллектора |
| `tempo.yml` | локальное хранение трейсов + TraceQL-метрики на local-blocks |
| `grafana/provisioning/` | автоматически настраиваемые источники данных + дашборды LMCache |

## Остановка

```bash
bash scripts/start-observability.sh --down
```

Тома данных (`prometheus_data`, `tempo_data`, `grafana_data`) сохраняются;
удалите их вручную через `docker volume rm`, если нужен чистый старт.

## Атрибуция

Конфигурация стека и дашборды происходят из официального репозитория LMCache
(`LMCache/LMCache`, Apache-2.0, `examples/observability`). Они включены в
репозиторий, чтобы `docker compose up -d` работал офлайн из этой копии.