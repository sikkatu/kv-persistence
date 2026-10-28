> 🌐 Языковые версии: [English](troubleshooting.md) | **Русский** | [中文](troubleshooting.zh.md)

# Устранение неполадок

Сначала три вещи:

```bash
bash scripts/budget-report.sh status        # занято, пространства, диск
tail -n 100 "$KVP_CACHE_ROOT/lmcache-server.log"   # сервис кэша
docker logs --tail 100 mimo-pro-tp4-kvp     # движок (лаунчер kv-persistence)
```

(`KVP_CACHE_ROOT` задаётся в `configs/generic/cache.env.local`.)

## Предпроверка жалуется на идентичность

`[!!] KVP_MODEL_REVISION must be a full 40-hex SHA` — допустимо ровно ДВА формата
(используйте один на проект):

1. **Локальный отпечаток файлов** (пишет `setup-identity.sh` по умолчанию):
   SHA-256 дайджест config.json + *.index.json + СОДЕРЖИМОГО каждого шарда.
   Это НЕ SHA коммита, но он идентифицирует реально лежащие файлы во
   побайтовый. Пересоздайте запуском `scripts/setup-identity.sh <каталог модели>`.
2. **SHA коммита HF** — только если вы осознанно отслеживаете происхождение:
   ```bash
   curl -fsSL https://huggingface.co/api/models/<your-org>/<your-model> \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
   ```
   Внимание: это HEAD удалённой ветки — НЕ обязательно ревизия ваших локально
   скачанных файлов (используйте только после сверки).

Оба пишутся в `configs/mimo-v2.6-pro/identity.env.local` (сайт-оверрайд,
.gitignore); отслеживаемый `identity.env` не меняется — `git pull` остаётся чистым.
Имя ветки не принимается никогда (только 40-hex).

`[!!] KVP_CHAT_TEMPLATE_DIGEST must be 64 hex` — выполните preflight с
`KVP_MODEL_DIR="$HOME/models/MiMo-V2.6-Pro-MOPD"`; digest считается по единому
алгоритму (`kvpersist.chat_template`) и сверяется с конфигом.

## CLI lmcache отсутствует или несовместим ABI

`ERROR: lmcache CLI missing` — установите по шагу 3 Развёртывания. Если
`import lmcache` падает с ошибками неопределённых символов, pip-wheel собран под
другой стек PyTorch/CUDA. Запасной путь — сервер прямо из production-образа:

```bash
docker run --rm --entrypoint lmcache "$IMAGE" server --help
```

Если так работает — подправьте строку запуска в `scripts/start-cache.sh` под
`docker run` с теми же аргументами (порты/пути не меняются).

## Сервис кэша запустился, но vLLM не подключается

1. Порядок старта: `start-cache.sh` ДО лаунчера vLLM
   (`run-vllm-with-persist.sh` иначе отказывается стартовать).
2. Смотрите строку ошибки в логе сервиса; оба процесса используют одну host-сеть —
   не переносите порт запросов без правки обеих сторон.
3. С интеграцией рецепта (`KV_PERSIST=1`): лаунчер печатает
   `KV_PERSIST=1: attaching LMCache connector …` — нет строки = ручка не дошла до
   лаунчера (проверьте `tp4.env` и экспортированы ли переменные).

## После перезапуска нет попаданий (TTFT не падает)

Проверяйте по порядку:

1. `cached_tokens` в ответе > 0 (если поле `null` — метрика недоступна, это
   отдельный случай: тест вернёт INCONCLUSIVE, а не безусловный confidence).
2. События чтения с L2/диска в логе растут (см. шаблон соответствия в
   `scripts/test-recovery.sh`) — они и есть доказательство дискового восстановления.
3. `used_bytes` НЕ выросло, пересоздавая те же данные
   (`bash scripts/budget-report.sh status` до/после).
4. Идентичность не менялась — повторите `scripts/preflight.sh`; смена ревизии/
   dtype/шаблона молча переводит в НОВОЕ пространство (по дизайну).
5. `KVP_PERSIST_ENABLED` равен `true` (иначе `start-cache.sh` предупреждает).

## Диск полон / записи отклоняются

`free_bytes: 0` в отчёте либо адаптер пишет отказ на жёстком лимите — поднимите
`KVP_CACHE_BUDGET_GB` или явно подчистите старые пространства. Не удаляйте
отдельные файлы внутри корня кэша, пока сервис работает.

## Эвикция слишком резвая (или наоборот)

Поднимите `KVP_EVICT_TRIGGER_WATERMARK` (начинать позже) или отрегулируйте
`KVP_EVICT_RATIO` (больше возврата за цикл). Проверяйте на маленьком изолированном
бюджете.

## Кэш мало переиспользуется (всё «холодное»)

Измерьте, какая часть промпта реально стабильна:

```bash
python3 "$REPO/implementation/prompt_probe.py" --tokenizer "$MODEL_DIR" \
  --request-file a.json --request-file b.json
```

Мало `stable_prefix_tokens` — динамические значения ломают префикс: лечите
сериализацию на источнике либо принимайте ограничение. Обрезать историю ради
кэша запрещено.

## Восстановление после повреждения файла

Повреждения терпимы: промах просто пересчитывает. Для очистки вручную: остановите
сервис кэша, `bash scripts/budget-report.sh purge --all --yes`, запустите снова.