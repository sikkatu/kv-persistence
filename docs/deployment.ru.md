> 🌐 Языковые версии: [English](deployment.md) | **Русский** | [中文](deployment.zh.md)

# Развёртывание

Линейный путь — без ручных дополнений. Команды рассчитаны на дословное копирование
на 4-GPU хост (Debian). Хост запускает production-рецепт из `sikkatu/recipes`;
данный проект его не изменяет.

## 0. Предусловия (хост)

* Linux-хост (любой дистрибутив) с NVIDIA GPU, Docker, `nvidia-smi`, закреплённый образ vLLM
  (`IMAGE` с `@sha256:` дайджестом) и каталог модели `MODEL_DIR`.
* Локальный NVMe со свободным местом не меньше `KVP_CACHE_BUDGET_GB` (по умолчанию
  1.5 ТБ) на конечном диске общего хранилища ВМ.
* Production-рецепт на диске; скрипты используют из него только `tp4.env.example`
  (дайджест `IMAGE`) и `patches/` + `MANIFEST.sha256` (монтируются только чтение).
* Python 3.12 (для `kvpersist` админа + скриптов и CLI `lmcache`).
  На Debian/Ubuntu (tested on Debian 12) системный Python — 3.11: установите интерпретатор 3.12 отдельно
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
$EDITOR configs/generic/cache.env.local        # KVP_CACHE_ROOT должен быть на общем диске ВМ
# identity.env.local создастся автоматически далее;
# правьте identity.env только для полей, фиксируемых вручную (например HF SHA).
```

**Значения идентичности заполняются и проверяются автоматически** — не собираем их
руками:

```bash
bash "$REPO/scripts/setup-identity.sh" "$HOME/models/<your-model-dir>"
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
curl -fsSL https://huggingface.co/api/models/<your-org>/<your-model> \
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

**Замечание для закреплённого CLI 0.5.6**: `lmcache server --l2-adapter`
принимает спецификацию адаптера как JSON-**строку**, а не путь к JSON-файлу.
`start-cache.sh` обрабатывает это автоматически (он передаёт отрендеренную
спецификацию инлайном), но при ручном запуске сервера передавайте
`--l2-adapter "$(cat l2-adapter.rendered.json)"`, а не
`--l2-adapter l2-adapter.rendered.json`.

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

### Lossless-кодек L2 (опционально)

По умолчанию уровень L2 хранит **байт-точные сырые** чанки
(`LMCACHE_L2_SERDE=none`) — идентично несжатому upstream-пути, вообще без
преобразования. Чтобы хранить их со lossless-сжатием (скромное, примерно
1.1–1.4x на реальных KV-данных), выберите кодек:

```bash
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh     # zstd
USE_LOSSLESS_SERDE=lz4 bash scripts/start-cache.sh   # lz4
```

или постоянно в `configs/generic/lmcache-server.env(.local)`:

```
### Кэш-сервер ОБЯЗАН работать под root (round 45)

Тест CUDA IPC границы (`bash scripts/ipc-test.sh`) доказал на целевом
хосте: контейнер движка (root) создаёт GPU-память, которую может открыть
только ROOT на хосте. Кэш-сервер не под root падает на
`register_kv_caches` с `CUDA_ERROR_MAP_FAILED`. Запускайте ОБА сервиса
через sudo:

```bash
sudo USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
sudo USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh
```

`start-cache.sh` сам находит python venv lmcache под sudo
(`SUDO_UID`/`SUDO_USER` -- ручные export не нужны). Файлы кэша будут
принадлежать root: `budget-report.sh` / `purge` тоже запускайте с sudo.bash
LMCACHE_L2_SERDE=zstd     # или lz4, или none
```

В проекте **нет lossy-квантизации** (fp8 / turboquant / lbq громко отвергаются
при старте). Кодек входит в namespace идентичности кэша: смена кодека превращает
ранее сохранённые чанки в безопасный cache miss (никогда не в неверное чтение),
поэтому используйте одно и то же значение во всех лаунчерах.

`KVP_L2_COMPRESS_LEVEL` (только zstd, дефолт 3, диапазон 1–19) меняет баланс
между коэффициентом сжатия и скоростью записи.

## 6. Запуск vLLM с постоянным KV-коннектором

```bash
export IMAGE='…@sha256:<digest>'
export MODEL_DIR="$HOME/models/<your-model-dir>"
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

### Prometheus + Grafana (опционально)

Полный стек наблюдаемости (OTel Collector + Tempo + Prometheus + Grafana)
лежит в `observability/` с уже предустановленными официальными дашбордами
LMCache:

```bash
bash scripts/start-observability.sh
# Grafana -> http://localhost:3000 (анонимный Admin, без входа)
```

Затем направьте сервис кэша на коллектор, чтобы пошли метрики
(`KVP_OTLP_ENDPOINT=http://localhost:4320` или короткая запись
`USE_OBSERVABILITY=1`). Телеметрия по умолчанию выключена: кэш полноценно
работает и без стека. Подробности и список панелей — в
`observability/README.ru.md`.
## Запуск сервиса кэша под systemd (опционально)

Вместо `scripts/start-cache.sh` (`setsid nohup`) сервис может работать как
systemd-юнит — переживает перезагрузку, перезапуск при сбое, логи в journald.
Юнит вызывает обёртку `kvp-lmcache-server`, передающую L2-спецификацию
JSON-строкой (закреплённый CLI LMCache 0.5.6 принимает `--l2-adapter` как
JSON-строку, а не путь).

```bash
sudo install -m 0755 systemd/kvp-lmcache-server /usr/local/sbin/kvp-lmcache-server
sudo install -m 0644 systemd/lmcache-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lmcache-server
journalctl -u lmcache-server -f
```

* Обёртка рендерит спецификацию из `configs/${KVP_MODEL_CONFIG:-mimo-v2.6-pro}/l2-adapter.json.in`,
  если отрендеренного файла нет (чистый хост) — юнит стартует и ДО первого
  `start-cache.sh`. Укажите `KVP_REPO_ROOT` в `/etc/kv-persistence.env`, если
  путь к репозиторию отличается от `/home/kvp/kv-persistence` (README
  клонирует в `~/kv-persistence`).
* Обёртка читает `cache.env`/`.local` для `KVP_CACHE_ROOT` и бюджета с той же
  приоритетностью, что `scripts/env.sh` (env побеждает файлы) — кастомный
  корень кэша из `cache.env.local` учитывается.
* Юнит работает от пользователя `kvp`; для по-инстансных пользователей —
  шаблонный юнит `lmcache-server@.service` (`User=%i` легален только там).
* **Права на корень кэша**: пользователь сервиса должен иметь право записи.
  Для пути по умолчанию `/var/lib/kv-persistence` это решает
  `StateDirectory=kv-persistence` в юните. Для кастомного `KVP_CACHE_ROOT`
  выполните один раз:
  ```bash
  sudo install -d -o kvp -g kvp -m 0700 /mnt/nvme/kvp   # пример пути
  ```
  Обёртка также проверяет доступность записи при старте и печатает эту команду,
  если каталог недоступен.
* НЕ запускайте одновременно юнит и `start-cache.sh` — конфликт за каталог
  кэша и порты.

## Остановка (движок и сервис кэша)
Движок и сервис кэша — РАЗНЫЕ процессы; останавливайте каждый явно:

* **Движок vLLM** (контейнер из `run-vllm-with-persist.sh`):
  `bash scripts/stop-engine.sh` (или `docker rm -f mimo-pro-tp4-kvp`).
  Ctrl-C во время работы лаунчера останавливает только ЛАУНЧЕР —
  контейнер продолжает работать; после Ctrl-C выполните `stop-engine.sh`.
* **Сервис кэша** (сервер lmcache): `bash scripts/stop-cache.sh`
  (корректный SIGTERM, файлы данных СОХРАНЯЮТСЯ).

Остановка любого из них НЕ удаляет постоянные данные кэша на диске.

## Откат (одна команда)

```bash
bash "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh"
```

Production-рецепт не изменён — его лаунчер работает в точности как раньше.
Постоянные KV-данные на общем диске ВМ НЕ удаляются — они просто игнорируются, пока выключены.
Остановить сервис кэша (сохраняя данные): `bash scripts/stop-cache.sh`.
Явно стереть данные кэша: `bash scripts/budget-report.sh purge --all --yes`
(ручные, явные действия — никогда автоматически).
