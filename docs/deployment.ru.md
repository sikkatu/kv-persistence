> 🌐 Языковые версии: [English](deployment.md) | **Русский** | [中文](deployment.zh.md)

# Развёртывание

Линейный путь — без ручных дополнений. Команды рассчитаны на дословное копирование
на 4-GPU хост (Debian). Хост запускает production-рецепт из `sikkatu/recipes`;
данный проект его не изменяет.

## 0. Предусловия (хост)

* Debian-хост с 4× RTX PRO 6000, Docker, `nvidia-smi`, закреплённый образ vLLM
  (`IMAGE` с `@sha256:` дайджестом) и каталог модели `MODEL_DIR`.
* Локальный NVMe со свободным местом не меньше `KVP_CACHE_BUDGET_GB` (по умолчанию
  1.5 ТБ) на закреплённом устройстве.
* Production-рецепт на диске; скрипты используют из него только `tp4.env.example`
  (дайджест `IMAGE`) и `patches/` + `MANIFEST.sha256` (монтируются только чтение).
* Python 3.12 (для `kvpersist` админа + скриптов и CLI `lmcache`).
  На Debian 12 системный Python — 3.11: установите интерпретатор 3.12 отдельно
  (например из bookworm-backports или через pyenv), `python3-venv` ставится
  отдельно; используйте `python3.12 -m venv` на шаге 3.

## 1. Клонируем ключевое приложение

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
export REPO="$PWD"
```

`REPO` используется всеми дальнейшими командами. Экспортированные переменные
(`REPO`, `IMAGE`, `MODEL_DIR`) живут только в их терминале shell: в новом терминале
повторите эти экспорты перед продолжением (это нормальное поведение shell).

## 2. Настройка

Все настройки сайта — в файлах `*.local`, которые важнее дефолтов и попадают в
.gitignore (`setup-identity.sh` создаёт `identity.env.local`, отслеживаемый
`identity.env` никогда не изменяется):

```bash
cp configs/generic/cache.env configs/generic/cache.env.local
$EDITOR configs/generic/cache.env.local        # KVP_CACHE_ROOT должен быть на NVMe
# identity.env.local создастся автоматически далее;
# правьте identity.env только для полей, фиксируемых вручную (например HF SHA).
```

**Значения идентичности заполняются и проверяются автоматически** — не собираем их
руками:

```bash
bash "$REPO/scripts/setup-identity.sh" "$HOME/models/MiMo-V2.6-Pro-MOPD"
```

Одна команда покрывает ВСЕ обязательные поля (`KVP_MODEL_REVISION`,
`KVP_TOKENIZER_REVISION`, `KVP_CHAT_TEMPLATE_DIGEST`) и записывает их в
`configs/mimo-v2.6-pro/identity.env.local` (без грязного git pull).
Скрипт отказывается угадывать неопределённые значения: подходят ровно два формата
REV-идентичности (используйте РОВНО ОДИН в каждом проекте):
1. **Локальный отпечаток кэша** (по содержимому shard-файлов) — безопаснее для хранения.
2. **SHA коммита HF** — если осознанно отслеживаете происхождение из HF; в этом случае
   команда показана ниже — но учтите: HEAD удалённой ветки уже мог не совпадать с
   локально загруженными файлами.

```bash
curl -fsSL https://huggingface.co/api/models/XiaomiMiMo/MiMo-V2.6-Pro-MOPD \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
```

## 3. Установка CLI LMCache

```bash
python3 -m venv ~/.venvs/lmcache
~/.venvs/lmcache/bin/pip install --upgrade pip
~/.venvs/lmcache/bin/pip install -r "$REPO/requirements-lmcache.txt"
export PATH="$HOME/.venvs/lmcache/bin:$PATH"
```

Версия LMCache **закреплена** в `requirements-lmcache.txt` (`lmcache==X.Y.Z`)
вместе с описанной комбинацией с образом vLLM — незакреплённая установка может
попасть в релиз, изменивший протокол MP-коннектора или поведение адаптера L2.
Не обновляйте lmcache без повторного прогона `scripts/test-recovery.sh`.
ВАЖНО: установка пакета ещё НЕ гарантирует совместимость ABI с вашим
образом (см. docs/compatibility.md) — окончательную приёмку проводят запуск
`lmcache server --help` / импорт пакета и полноценный сценарий восстановления на целевом оборудовании.

## 4. Предпроверка

```bash
bash scripts/preflight.sh
```

`preflight PASSED` — можно двигаться дальше. Каждое `[!!]` говорит, что именно
починить. С установленным `KVP_MODEL_DIR` предпроверка дополнительно пересчитывает
digest чат-шаблона по единому алгоритму и сверяет его со значением из конфига —
при несовпадении печатает команду `setup-identity.sh`, которая это исправляет.
Ручной операции копирования НЕ требуется.

## 5. Запуск сервиса постоянного кэша

```bash
bash scripts/start-cache.sh
```

Создаётся `KVP_CACHE_ROOT` (chmod 700), отрисовывается адаптер L2 и запускается
`lmcache server` отвязанно (`setsid nohup`; pid-файл и лог — под `KVP_CACHE_ROOT`).
Ранее закэшированные данные сохраняются — автоматической очистки при старте нет.

## 6. Запуск vLLM с постоянным KV-коннектором

```bash
export IMAGE='…@sha256:<digest>'
export MODEL_DIR="$HOME/models/MiMo-V2.6-Pro-MOPD"
bash scripts/run-vllm-with-persist.sh
```

Скрипт повторяет аргументы продового лаунчера рецепта и добавляет
`--kv-transfer-config`, указывающий на работающий lmcache-сервер
(MP-режим, `LMCacheMPConnector` через `kv_connector_module_path`).
Патчи рецепта проверяются по `MANIFEST.sha256` и монтируются только на чтение.

## 7. Проверка

```bash
bash scripts/verify-engine-identity.sh      # обязательный барьер: геометрия движка == пины
bash scripts/test-recovery.sh               # приёмка: см. docs/benchmarks.md
```

## Мониторинг

```bash
bash scripts/budget-report.sh status      # занято, пространства, диск
bash scripts/budget-report.sh quota       # квоты по пространствам
bash scripts/budget-report.sh purge --help
```

## Откат (одна команда)

```bash
bash "$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh"
```

Production-рецепт не изменён — его лаунчер работает в точности как раньше.
Постоянные KV-данные на NVMe НЕ удаляются — они просто игнорируются, пока выключены.
Остановить сервис кэша (сохраняя данные): `bash scripts/stop-cache.sh`.
Явно стереть данные кэша: `bash scripts/budget-report.sh purge --all --yes`
(ручные, явные действия — никогда автоматически).
