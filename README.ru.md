> [English](README.md) | **Русский** | [中文](README.zh.md)

# kv-persistence — универсальный постоянный KV-кэш для vLLM

<p align="center">
  <img src="docs/assets/og-template.jpg" alt="kv-persistence — постоянный KV-кэш для vLLM на общем диске ВМ" width="820">
</p>

Постоянный слой KV-кэша на диске для vLLM: после перезапуска переиспользуемые
KV-блоки промптов восстанавливаются с NVMe, а не пересчитываются. Целевой
эффект — длинные беседы в чат-клиенте (100 тыс.–1 млн токенов) открываются за секунды,
а не за десятки минут — измеряется на целевом оборудовании скриптом
`scripts/benchmark.sh`; результаты публикуются в
[docs/benchmarks.md](docs/benchmarks.md) по завершении приёмочного прогона
(см. [Статус](#статус)).

**📖 Документация (EN / RU / ZH) — в Wiki:**
[Главная](https://github.com/sikkatu/kv-persistence/wiki) ·
[Быстрый старт](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start.ru) ·
[Архитектура](https://github.com/sikkatu/kv-persistence/wiki/Architecture.ru) ·
[Конфигурация](https://github.com/sikkatu/kv-persistence/wiki/Configuration.ru) ·
[Персистентность и восстановление](https://github.com/sikkatu/kv-persistence/wiki/Persistence-and-Recovery.ru) ·
[Хранилище и эвикция](https://github.com/sikkatu/kv-persistence/wiki/Storage-and-Eviction.ru) ·
[Интеграция с рецептами](https://github.com/sikkatu/kv-persistence/wiki/Recipes-Integration.ru) ·
[Безопасность](https://github.com/sikkatu/kv-persistence/wiki/Security.ru) ·
[Устранение неполадок](https://github.com/sikkatu/kv-persistence/wiki/Troubleshooting.ru) ·
[FAQ](https://github.com/sikkatu/kv-persistence/wiki/FAQ.ru)

Работает с **любой моделью на vLLM на любом GPU-хосте** (тестовый референс:
MiMo-V2.6-Pro на 4× NVIDIA RTX PRO 6000 через рецепт `sikkatu/recipes`).
Слой кэша модельно-агностичен; специфичные для модели настройки лежат
в `configs/<model>/` — скопируйте эту папку для новой модели и заполните пины.

## Что делает

```
клиент чата → vLLM (TP4) → KV-коннектор (LMCacheMPConnector)
       → lmcache-сервер (отдельный процесс) → L2 (nixl_store_dynamic, POSIX) → NVMe
```

* **Персистентность при рестартах** — данные переживают рестарт vLLM И полный
  рестарт сервиса кэша (`persist_enabled=true`; дисковый индекс лениво
  восстанавливается из имён файлов).
* **Ограниченное хранилище** — жёсткий лимит (1.5 ТБ по умолчанию, настраивается под
  объём NVMe), мягкий порог (90 %) с автоматической LRU-эвикцией; кэш никогда сам себя
  не стирает при старте.
* **Пространства совместимости** — ревизия модели, токенизатор, KV-тип, геометрия
  блоков и чат-шаблон входят в ключ кэша: несовместимые конфигурации никогда не
  читают чужие KV-данные; неопределённость даёт промах, а не неверное попадание.
* **Модельно-агностичное ядро** — политика кэша отделена от интеграции с моделью;
  новая модель добавляет конфигурацию, а не код. KV-данные разных моделей не
  делятся никогда.

## Карта документации

| Тема | Wiki (EN/RU/ZH) | В репозитории (EN) |
|---|---|---|
| Установка → настройка → запуск → проверка | [Быстрый старт](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start.ru) | [docs/deployment.md](docs/deployment.md) |
| Топология, поток данных, модель отказов | [Архитектура](https://github.com/sikkatu/kv-persistence/wiki/Architecture.ru) | [docs/architecture.md](docs/architecture.md) |
| Все параметры, лимиты, квоты | [Конфигурация](https://github.com/sikkatu/kv-persistence/wiki/Configuration.ru) | [docs/storage-management.md](docs/storage-management.md) |
| Приёмочные сценарии A/B | [Персистентность и восстановление](https://github.com/sikkatu/kv-persistence/wiki/Persistence-and-Recovery.ru) | [docs/benchmarks.md](docs/benchmarks.md) |
| `KV_PERSIST=1` в лаунчерах рецепта | [Интеграция с рецептами](https://github.com/sikkatu/kv-persistence/wiki/Recipes-Integration.ru) | [docs/compatibility.md](docs/compatibility.md) |
| Правила безопасности | [Безопасность](https://github.com/sikkatu/kv-persistence/wiki/Security.ru) | [docs/security.md](docs/security.md) |
| Честный статус и пробелы | — | [docs/final-report.md](docs/final-report.md) |

## Быстрый старт

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
bash scripts/setup-identity.sh "$HOME/models/<your-model-dir>"   # заполнение И проверка пинов идентичности
bash scripts/preflight.sh               # проверка хоста, хранилища и идентичности
bash scripts/start-cache.sh             # сервис постоянного кэша (NVMe)
bash scripts/run-vllm-with-persist.sh   # vLLM + KV-коннектор (нужны IMAGE, MODEL_DIR)
bash scripts/test-recovery.sh           # приёмка: рестарт → доказательство переиспользования (exit 0 = PASS)
```

Каждая команда громко отказывается вместо полурабочей установки: значения
идентичности записывает и проверяет `setup-identity.sh`, `preflight.sh`
контролирует физическое NVMe-устройство, а `test-recovery.sh` завершается
машинным вердиктом.

Постоянный переключатель в production-рецепте — `KV_PERSIST=1` в `tp4.env`
(см. вики «Интеграция с рецептами»); откат — удаление этой одной строки.

## Структура репозитория

| Путь | Содержимое |
|---|---|
| `docs/` | англоязычная техническая документация (все языки — в вики) |
| `configs/generic/` | настройки кэша и lmcache-сервера, EN/RU/ZH-переводы комментариев |
| `configs/mimo-v2.6-pro/` | пины идентичности MiMo и шаблон L2-адаптера, EN/RU/ZH |
| `scripts/` | preflight, старт/стоп кэша, лаунчер, тест восстановления, бенчмарк, админ-CLI |
| `implementation/` | пакет `kvpersist`: идентичность, бюджеты, ранжирование, админ-CLI |
| `tests/` | unit (59, где угодно), интеграция (нужен `lmcache`), восстановление (GPU-хост) |

## Статус

Реализация завершена; офлайн-тесты зелёные. Приёмочные hardware-прогоны на хосте с
GPU-хосте заскриптованы (`scripts/test-recovery.sh`, `scripts/benchmark.sh`) и выполняются
эксплуатирующей стороной. Непроверенные возможности помечены `NEEDS TESTING` /
`BLOCKED` в [docs/final-report.md](docs/final-report.md) — непроверенное не
выдаётся за работающее.

## Правила проектирования

* Production-рецепт (`sikkatu/recipes`) для проекта **только для чтения**.
* Никаких неаутентифицированных эндпоинтов управления; корень кэша `0700`.
* В дереве нет учётных данных, токенов, текстов промптов и данных кэша.
* Все задокументированные команды работают как есть после экспорта
  переменных-предусловий (`IMAGE`, `MODEL_DIR`, `API_KEY`) — «Быстрый старт»
  даёт линейный порядок.

## Как подключить свою модель

Слой кэша модельно-агностичен — добавление модели НЕ требует правок кода:

1. Скопируйте референсный пример и переименуйте в id вашей модели:
   ```bash
   cp -r "$REPO/configs/mimo-v2.6-pro" "$REPO/configs/<ваша-модель>"
   ```
2. Отредактируйте `configs/<ваша-модель>/identity.env`:
   * `KVP_MODEL_ID` = HF-id вашей модели (или любое уникальное имя).
   * `KVP_TP_SIZE`, `KVP_BLOCK_SIZE`, `KVP_NUM_KV_HEADS`, `KVP_HEAD_DIM`,
     `KVP_MAX_MODEL_LEN`, `KVP_KV_CACHE_DTYPE` = KV-геометрия, которую
     vLLM отчитывает для ВАШЕЙ модели (см. `docs/architecture.md`,
     «identity fields»).
   * `KVP_CONNECTOR_VERSION` = дескриптивный тег вашей связки vLLM+LMCache.
3. Запустите `bash scripts/setup-identity.sh "$HOME/models/<ваша-модель>"` —
   ревизии/digest заполнятся автоматически.
4. Направьте `scripts/preflight.sh`, `start-cache.sh` и vLLM-лаунчер на
   `configs/<ваша-модель>/` (скрипты читают путь из `CONFIG_FILE`/env; см.
   `docs/deployment.md`).

Каждая модель получает собственное пространство имён кэша — KV-данные разных
моделей никогда не смешиваются, а изменение любого поля идентичности безопасно
переводит в промах кэша.

**Примечание:** поддержка моделей кроме MiMo не тестировалась до прогона на
железе; рассматривайте `configs/mimo-v2.6-pro/` как единственный проверенный
референс и ожидайте адаптации лаунчера (патчи/загрузчик модели) для
архитектурно отличающихся моделей.

## Лицензия

Apache-2.0 — см. [LICENSE](LICENSE). Если вы **распространяете** модифицированную
версию, форк, бандл или SaaS на основе этого кода, вы обязаны нести содержимое
[NOTICE](NOTICE) (автор: **sikkatu** / проект: **kv-persistence** /
https://github.com/sikkatu/kv-persistence), согласно разделу 4(d) Apache-2.0.
Запуск без модификаций во внутреннем бэкенде не требует атрибуции — §4(d)
распространяется на дистрибьюторов, а не на каждого внутреннего пользователя.