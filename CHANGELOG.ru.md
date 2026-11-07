# Журнал изменений

Все заметные изменения kv-persistence, новые сверху. Здесь хранятся только
последние 3 раунда (запрос пользователя) — полная история правок в
git-логе ветки `main`.

Версии языков: [English](CHANGELOG.md) | **Русский** | [中文](CHANGELOG.zh.md)

## Не выпущено

Runtime: закреплённый образ vLLM (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Раунд 57 (вердикт + честность доков)

* Вердикт приёмки восстановления — **PASS** в docs/final-report.*:
  авто-определение устройства кэша (раунд 55) делает независимые счётчики
  NVMe-байтов доступными в том же прогоне — ручной `KVP_CACHE_DEVICE` не
  нужен (переопределение доступно).
* CHANGELOG больше не содержит дословных цитат запросов — изменения
  описываются напрямую.
* Документация `start-all.sh` / `stop-all.sh` и общего API-ключа
  (`$HOME/.config/kvp/api_key`) добавлена в README обоих репо (EN/RU/ZH).

## Раунд 56 (общий API-ключ + динамический hotsplit по умолчанию)

* ОДИН API-ключ для ОБОИХ репо: оба лаунчера (kv-persistence + recipes run-tp4-hotsplit.sh /
  run-tp4-uva.sh) читают и пишут канонический файл
  `$HOME/.config/kvp/api_key` (переопределение: `KVP_API_KEY_FILE=`).
  Легаси-файл `live/api_key` мигрируется при первом запуске и остаётся
  зеркалом для старого инструментария.
* Динамические горячие/холодные эксперты по умолчанию в kv-persistence:
  лаунчер теперь монтирует `/w`-механизм рецепта (hotsplit.py,
  hotsplit_plan.py, numa_bind.py, sitecustomize.py, expert_hist_mix.json)
  и передаёт `PYTHONPATH=/w` + дефолты HOTSPLIT_* рецепта
  (HOTSPLIT_DYNAMIC=1, HOT_GIB=30). Отключение: `KVP_HOTSPLIT=0`.
* CHANGELOG обрезан до последних 3 раундов — старые заметки в git-логе
  ветки `main`.

## Раунд 55 (авто-определение KVP_CACHE_DEVICE)

* `KVP_CACHE_DEVICE` больше не нужно подбирать вручную (дружелюбно к VM и LUKS):
  общий хелпер
  `_kvp_resolve_cache_device` в env.sh определяет устройство из
  `live/cache_device` (запоминается) или `findmnt SOURCE` корня кэша —
  работает с дисками VM (`/dev/vda1`), LUKS-mapper-узлами
  (`/dev/mapper/cryptroot`, через readlink) и NVMe-разделами. Используется в
  test-recovery.sh и preflight.sh; явный пин имеет приоритет. Выбор
  записывается в `live/cache_device`.
* Остатки в docs/final-report.md вычищены: строки LMCache runtime, version-pin
  и serde end-to-end теперь VERIFIED (железо 2026-11-07).

> Старые заметки по раундам обрезаны 2026-11-07: полная история правок — в git-логе ветки `main`.
