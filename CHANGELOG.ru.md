# Журнал изменений

Все заметные изменения kv-persistence, новые сверху. Здесь хранятся только
последние 3 раунда (запрос пользователя) — полная история правок в
git-логе ветки `main`.

Версии языков: [English](CHANGELOG.md) | **Русский** | [中文](CHANGELOG.zh.md)

## Не выпущено

Runtime: закреплённый образ vLLM (`@sha256:167ccd86…`) + **кастомная сборка
LMCache** (база `0.5.7.dev428` = `03ee5b82418b6bd44e424cdd46932d9346f43eb2`,
downstream-патчи на апстрим PR #4136 и #5972, см. `docs/lmcache-build.ru.md`);
прежний пин **LMCache 0.5.6** заменён этой кастомной сборкой. В
`requirements-lmcache.txt` теперь указывается производный образ.
### Раунд 20 (выбор образа запуска, кодеки в образе, идентичность сборки в namespace кэша)
**Факты с железа (лог оператора, 20 ноября 2026):** полная сборка C0–C16 прошла (wheel `0.5.8rc1.post48+kvp.60dce759ea93`, C11 = 155 passed / 0 skipped), но цепочка запуска сломалась в трёх местах: активный `IMAGE=` (stock-пин) в `tp4.env` затенял `live/image` (до отказа поднялись 6 контейнеров observability), `run-tp4-hotsplit.sh` перезаписывал `live/image` stock-дайджестом ДО verify-гейта (кастомный образ «забывался» после одного неудачного запуска), а docker-путь кэша падал на `ModuleNotFoundError: No module named 'zstandard'` — дефолтный кодек zstd никогда не был в образе, docker-путь не мог работать ни разу.
* `ba84356` **K1** (P0, один fail-fast выбор образа запуска): `_kvp_resolve_runtime_image` в `env.sh` разрешает образ запуска ОДИН раз, до шага 1/3 и до любого вызова docker, во всех трёх лаунчерах (`start-all.sh`, `start-cache.sh` в docker-режиме, оба лаунчера рецепта только при `KV_PERSIST=1`). Stock-`IMAGE=` — это не ошибка пользователя, а base-пин: он обязан совпасть с label `org.kvp.lmc-base` кастомного образа из `live/image` (сравнение по digest через `check_image_identity --expected-base`; тот же digest под другим тегом проходит, другой digest под тем же тегом отклоняется), а запускается КАСТОМНЫЙ образ. Явный override `sha256:<64hex>` по-прежнему побеждает (контракт README). `KV_PERSIST!=1` (stock-прогоны рецепта) не трогает `IMAGE` вообще; `KVP_PERSIST_ENABLED=false` пропускает разрешение целиком (регрессия round-81b, найдена набором recipes и исправлена). Тестовый opt-out `KVP_SKIP_IMAGE_CHECK=1` работает. Матрица разрешения (a)–(h) проверена на РЕАЛЬНЫХ скриптах с логирующей docker-заглушкой, отвечающей labels/identity из реальных SHA патчей; 4 поведенческих мутации — красные.
* `271cb5c` (recipes) + `bc131af` (recipes) **K2** (P0): `live/image` пишется ТОЛЬКО после успешной проверки кастомного образа и только при `KV_PERSIST=1` без `KVP_SKIP_IMAGE_CHECK` — два места, которым позволено писать файл: `build-image.sh` (после своего verify-гейта) и лаунчер (после deep-проверки). Sentinel-тесты: отказ на stock не меняет файл ни на байт, успешный кастомный запуск записывает точный id, `KV_PERSIST=0` и `KVP_SKIP_IMAGE_CHECK=1` не трогают файл; восстановление старого порядка → красный.
* `6bfb6fc` **K3** (P0, pre-flight в docker-режиме): проверка теперь сверяет, что кэш-контейнер и движок работают с ОДНИМ образом (`.Image` против `.Id`), вместо сравнения версии lmcache в host-venv с версией в образе (что отказывало в каждом запуске, если в host-venv стояла любая другая сборка). RPC-ping выполняется ВНУТРИ кэш-контейнера (exit 3 = отказ там); подсказка для host-режима называет реальный путь к wheel с `--force-reinstall --no-deps` (подсказка `pip install lmcache==...+kvp` вела на несуществующий на PyPI пакет). Мутации (возврат host-сравнения / убранный exec-ping / сравнение `.Id` с именем тега) → красные.
* `56662a2` **K4** (P1, закрыты дыры тестов r19): сгенерированный Dockerfile разбирается структурно (склейка `\`-продолжений, разделение RUN по `&&`, `shlex.split`) — COPY src == реальное имя wheel, COPY dst == путь установки pip == путь rm -f, имя проходит `packaging.utils.parse_wheel_filename`, `--no-deps` присутствует; исполняемый тест запускает РЕАЛЬНУЮ команду pip install, вырезанную из Dockerfile, на минимальном wheel с реальными cp312-тегами; тесты цепочки запуска усилены. 4 мутационных детектора (только COPY dst / только путь pip / только путь rm / убранный `--no-deps`) → красные.
* `2b51670` **K6** (P0, кодеки входят В образ): `requirements-image-codecs.txt` (ЕДИНСТВЕННЫЙ источник пинов — `zstandard==0.25.0`, `lz4==4.4.5`, cp312 manylinux x86_64 wheels с PyPI, точные `--hash=sha256`) копируется в контекст сборки и устанавливается с `--no-deps --only-binary=:all: --require-hashes`; SHA файла и пиновые версии попадают в `/etc/kvp-lmcache-identity.json` и `image-identity.env` (`image_codecs_sha256` / `KVP_IMAGE_CODECS_SHA256`). `_verify_image.py` печатает СТРОГИЕ маркеры `KVP_ZSTD_IMPORT=OK` / `KVP_LZ4_IMPORT=OK` (версии — отдельными строками `KVP_*_VERSION=`), без них `build-image.sh` отказывает. Отказ `start-cache.sh` по кодеку теперь говорит «пересоберите образ» вместо невозможного «установите кодек в образ». Мутации (убрать `--require-hashes` / снять пин `==` / убрать пакет / убрать COPY / вырезать маркеры из verify-вывода) → красные. **Честный предел тестов:** то, что `pip` реально ставит пакеты в образ, проверяет только железо — серверный smoke при сборке (ниже) запускает НАСТОЯЩИЙ сервер в образе и ловит сбои кодеков/nixl/адаптера, но тоже только на реальной GPU-сборке.
* `fc8a78b` + `eedd109` **K6.3** (P1, серверный smoke при сборке образа): настоящий cache-сервер обязан СТАРТОВАТЬ в свежесобранном образе. ЕДИНЫЙ источник команды: `env.sh` добавляет `_kvp_render_l2_adapter` + `_kvp_server_argv`, их используют ОБА — `start-cache.sh` (продакшен) и `scripts/_smoke_cache_server.sh` (запускается ВНУТРИ образа из `build-image.sh`). Для ДВУХ кодеков (zstd по умолчанию, none raw): отрендерить адаптер, поднять сервер, дождаться `kvpersist.mp_ping`, SIGTERM, чистый выход. Строгие маркеры `KVP_SMOKE_<CODEC>_READY=OK` / `KVP_SMOKE_<CODEC>_EXIT=0`; без них гейт сборки отказывает. Мутация: smoke без маркеров → отказ (красный детектор).
* `d5fc90b` + `767826e` **K7** (P0, namespace кэша должен различать сборки LMCache): `CacheIdentity` получил ОБЯЗАТЕЛЬНОЕ поле `lmcache_build` (= `wheel_version` из `/etc/kvp-lmcache-identity.json`, `0.5.8rc1.post48+kvp.<12hex>`); пустое/нечитаемое → `IdentityError` (неопределённость → cache miss). До этого stock-сборка 0.5.6 и кастомная давали ОДИН namespace (`kvp-7a26d4bf...`) и новый сервер читал старый кэш. ЕДИНЫЙ источник для всех потребителей: `kvpersist.identity.read_lmcache_build()` (bash читает то же значение через `check_image_identity.py --print-wheel-version`, так что bash и Python не могут разойтись); `verify-engine-identity.sh` кросс-проверяет пин `KVP_LMCACHE_BUILD` с `wheel_version` работающего образа (MISMATCH → exit 4). Тесты: пустая сборка → `IdentityError`, разные сборки → разные namespace, хешируется значение из identity JSON, кросс-проверка гейта на РЕАЛЬНОМ скрипте с docker-заглушкой, мутация без поля в хешируемой нагрузке → красная.
* `6dd98f0` (recipes): фикстуры тестов несут новое обязательное поле и записывают `live/image-identity.env` ровно так, как `build-image.sh` делает на реальной машине.
* Полный прогон: kv-persistence **704 passed, 26 skipped** (все skip — окружение: promtool, nginx, zstd/lz4, lmcache CLI, live-LMCache, только железо); recipes **448 passed, 5 skipped** (после обновления фикстур K7; 443/5 было до изменения фикстур этого раунда).
### Раунд 19 (шаг «образ»: имя wheel в Dockerfile, identity как env-файл)
**Факты с железа (логи оператора, 20 ноября 2026)** — вместо формулировки раунда 18 «подтверждение на железе в ожидании»:
* быстрый прогон `scripts/rerun-lmcache-gate.sh --trust-wheel` на wheel из 4-го запуска: C8-C12 прошли, C11 = 155 passed / 0 failed / 0 errors / 0 skipped;
* полный `scripts/build-lmcache.sh`: C0-C16 прошли, wheel `0.5.8rc1.post48+kvp.60dce759ea93`, sha256 `daff4af1016e956c351f054dd67d6f45d85a5c4c6165b0f0867833cdd56b118a`, C11 = 155 passed / 0 skipped, C14 «engine identity OK», C13 (GPU) пропущен по умолчанию (KVP_BUILD_GPU_TESTS не задан), последняя строка `DONE`;
* `scripts/build-image.sh`: все проверки целостности раунда 18 прошли, `docker build` упал на шаге [4/4] (H1 ниже). Образ НЕ собран; GPU-проверка в образе не запускалась.
**Всё ещё ожидают железа:** build-image.sh до конца (GPU-проверка в образе, live/image), workload-тест фикса incomplete-KV, GPU-регрессия C13 по желанию.
* `666e322` **H1** (P0, образ не собирался): Dockerfile переименовывал wheel в `/tmp/kvp-lmcache.whl`; pip разбирает имя локального `.whl` как `{name}-{version}-{python}-{abi}-{platform}.whl` и отверг его — `ERROR: Invalid wheel filename (wrong number of parts): 'kvp-lmcache'` на шаге [4/4] `docker build`. Теперь wheel сохраняет РЕАЛЬНОЕ имя в образе (`WHEEL_BASENAME` используется в COPY, pip install и rm; путь в кавычках, локальная версия содержит `+`). Тесты (test_r19_h1_wheel_name.py): basename пути установки == реальное имя wheel и проходит packaging.utils.parse_wheel_filename; исполняемая установка минимального wheel с тем же name-version работает, имя `kvp-lmcache.whl` падает с точной ошибкой железа; фикстура — имя как у настоящей сборки (с `+kvp.60dce759ea93`, теги cp312). До правки красный (3 failed на HEAD), после — зелёный; мутация (вернуть фиксированное имя) → красный.
* `256cd70` **H2**: image-identity.env теперь настоящий env-файл — только строки `^KVP_[A-Z0-9_]+=` (сырой вывод проверки содержит шум лога и строки `KVP_GPU_SMOKE[N]` с недопустимыми для shell именами, ломавшие `set -e; . live/image-identity.env`). Сырой вывод сохраняется в live/image-verify.txt; в .env остаются агрегаты (KVP_GPU_COUNT, KVP_GPU_SMOKE_OK_COUNT, KVP_GPU_CC). Тест: реалистичный «шумный» verify.txt, source под `set -e` проходит и KVP_CUSTOM_IMAGE_ID переживает round-trip; сырой лог сохранён отдельно. До правки красный (2 failed на HEAD), после — зелёный; мутация (cat verify.txt в .env) → красный.
* `1fe83bb` **H4.1**: тест r18 G1 запускал build-image.sh на ДРУГОМ, пустом каталоге (KVP_BUILD_DIR=build против KVP_OUT_DIR=out сборки) — отказ `provenance.json not found` срабатывал ВСЕГДА, даже без фикса. Теперь оба скрипта работают с ОДНИМ каталогом (как production live/lmcache-build), плюс сквозной тест: настоящая сборка → её собственный provenance → настоящий build-image доходит до docker build → упавшая пересборка → отказ. Мутация (убрать rm) → оба контроля красные.
* `1a5ae22` **H4.2**: _rerun_incontainer.sh печатал `ownership returned` даже при неудавшемся chown (`|| true` глотал ошибку). Теперь неудачный chown печатает `WARNING: could not return ownership (rc=N): the work dir may stay root-owned` и никогда не заявляет успех; код выхода скрипта не меняется. Тест: настоящая `_kvp_chown_back` с падающей заглушкой chown; мутация (безусловный echo) → красный.
* `01f1092` **H4.3**: verify-engine-identity.sh теперь ГРОМКО предупреждает `WARNING: this image carries an UNTESTED LMCache wheel`, если в запечённом identity непустое поле `untested_wheel` (образы с KVP_ALLOW_UNTESTED_WHEEL=1); пустое/отсутствующее поле (старые образы) молчит. Печатается ДО тяжёлых проверок — видно при любом исходе. Тест: настоящий скрипта гейта с тремя вариантами identity; мутация (подавить предупреждение) → красный.
* Полный прогон: kv-persistence **669 passed, 26 skipped** (все скипы — окружение); recipes **443 passed, 5 skipped** (код в этом раунде не менялся).
### Раунд 18 (целостность шага «образ»: provenance, wheel, патчи)
* `9030142` **G1** (упавшая сборка оставляла старый provenance.json рядом с новым непротестированным wheel): wheel попадает в `$OUT_DIR/wheels` ещё на C5 (задолго до гейтов C8-C14), а provenance.json писался в самом КОНЦЕ успешной сборки — после любой упавшей на гейте сборки build-image.sh собрал бы образ из несогласованной пары. Теперь build-lmcache.sh удаляет provenance.json в начале сборки (рядом с rerun-prod-hash.txt). Тесты: настоящая сборка + docker-заглушка (rc=1 после кладения wheel) → provenance нет, настоящий build-image.sh отказывает 'provenance.json not found'; позитивный контроль пишет новый. Мутация (убрать rm) → красный.
* `90984c1` **G2** (build-image.sh не сверял wheel и патчи с provenance): теперь ДО docker build — ровно один *.whl (ноль/больше одного → отказ), sha256(WHEEL) == wheel_sha256 из provenance (`wheel does not match provenance.json (rebuild: bash scripts/build-lmcache.sh)`), SHA патчей pr4136/pr5972 == текущим файлам патчей (`patches changed since the wheel was built: full rebuild required`), tests_skipped обязан быть "0" (иначе отказ; с KVP_ALLOW_UNTESTED_WHEEL=1 — громкое предупреждение, identity помечается `untested-wheel`). SHA патчей в identity образа берутся из PROVENANCE, а не из текущих файлов (прежний источник правды заставлял метку ВРАТЬ). Тесты: два/ноль wheel, изменение wheel на один байт, правки 4136/5972, отказ/разрешение tests_skipped, согласованная пара — каждый с мутационным детектором на настоящем скрипте.
* `03fc6bd` **G3**: (1) дублирующиеся ключи в heredoc provenance убраны (7 ключей писались дважды; строгий тест object_pairs_hook на настоящей сборке); (2) gate_common prod-hash использует `--name-only -z` + core.quotepath=off — имена файлов с пробелами/не-ASCII (`a b.py`, `тест.py`) теперь корректно влияют на хэш (тесты на настоящих деревьях); (3) перезапуск гейта возвращает права рабочих каталогов: rerun-lmcache-gate.sh передаёт KVP_HOST_UID/KVP_HOST_GID, _rerun_incontainer.sh делает chown обратно через fail-soft trap на EXIT (выполняется даже при падении гейта).
* `202a2a5` продолжение: старые harness'ы build-image (test_live_image_record, test_base_image_pin, test_docker_stdin_delivery) получили реалистичные фикстуры provenance (как у настоящей сборки), чтобы проверки G2 проходили на согласованных парах.
* Полный прогон: kv-persistence **657 passed, 26 skipped** (все скипы — окружение); recipes **443 passed, 5 skipped** (код в этом раунде не менялся). Статус железа без изменений: C0-C10 passed on the operator's host (new base 07f1c6bb6), C11 (4136 test fixtures) fixed in r16 («проверено без железа, подтверждение на железе в ожидании»), C12-C16 pending.
### Раунд 16 (фикстуры 4136, junit-гейты, слепая зона engine-pin, быстрый перезапуск)
* `c354ba9` **P0** (лог оператора 19.11 18:53: C0-C10 прошли на новой базе,
  C11 = 150 passed / 5 failed): все пять падений в
  `tests/v1/test_lmcache_mp_connector.py` (файле, который ДОБАВЛЯЕТ патч
  4136) — апстрим-тесты написаны против августовской головы PR и строили
  коннектор через `__new__`, задавая только часть текущих атрибутов `__init__`
  (`_delay_free_req_ids`, `_can_store`, `_pinned_boundary_state_blocks`,
  `_lazy_offload_manager`); в заглушках запросов не было
  `num_computed_tokens`; один тест проверял устаревший API `_pending_store`
  (в продакшне его уже нет). Только фикстуры (продакшн не тронут, проверено
  побайтово): хелпер задаёт четыре атрибута, заглушки несут
  `num_computed_tokens=0` / `[0]` с усиленными проверками
  `update_num_engine_computed_tokens(0, 32)`, lazy-offload-тест переписан
  под API менеджера (`on_request_finished` -> `sessions_to_end=[]`,
  `end_session` не вызывается). Патч переэкспортирован из реального
  патченого дерева (git diff, хунки не правились руками); SOURCES.md
  записана адаптация + новый SHA-256. Ожидаемый C11 на железе: 155 passed,
  0 skipped. НОВЫЙ статический тест (test_r16_patch4136_fixtures.py):
  атрибуты фикстур в добавленных строках, нет `_pending_store`, SOURCES.md
  несёт актуальный хеш патча. Мутация: вернуть `_pending_store` → 2 теста
  КРАСНЫЕ.
* `b589b98` **P1** (скрытый дефект, проверен заранее): pytest.ini LMCache
  содержит `log_cli = true`, и реальный pytest декорирует итоговую строку
  символами `=` — старый `grep -E '^[0-9]+ (passed|...)'` не находил НИЧЕГО
  на ЗЕЛЁНОМ прогоне → «pytest summary not found» на здоровом наборе
  (стабы r11-r15 печатали недекорированную строку, поэтому тесты кодера этого
  не видели). C12/C13 теперь считают по отчёту `--junitxml` через общий
  модуль (tests/failures/errors/skipped суммируются по всем `<testsuite>`,
  passed = tests - failures - errors - skipped; rc==0 И failures==0 И
  errors==0 И skipped==0 И passed>0; отсутствующий/пустой/битый XML → явный
  `REGRESSION FAILURE: junit report missing/unparsable`). Текстовый grep
  убран; строку итога собирает сам скрипт (`CPU gate PASSED (155 passed,
  0 failed, 0 errors, 0 skipped)`). НОВЫЕ тесты (test_r16_c12_junit_gate.py)
  с РЕАЛЬНЫМ pytest в ОБЕИХ конфигурациях (с pytest.ini в стиле LMCache
  log_cli и без): зелёный проходит, skip отвергается со строками SKIPPED,
  падение отвергается, отсутствующий XML отвергается, плюс статический гейт
  «нет текстового grep». Мутация: вернуть текстовый grep → 5 тестов КРАСНЫЕ.
* `d43576e` **P2** (скрытый дефект): `engine_pins` матчил только строки
  `name==ver` — строка прямой ссылки freeze (`vllm @ file:///...`) молча
  опустошала обе стороны, и проверка «движок не изменился» проходила слепо.
  C14 теперь разбирает ОБА формата freeze (`name==ver` + `name @ url`, имена
  нормализуются по PEP 503, прямые ссылки пинуются как `ref:<url>`),
  ТРЕБУЕТ torch и vllm в baseline freeze (иначе `BUILD REFUSED: engine pins
  not found in baseline freeze (format not recognised)` + первые 20 строк
  baseline), и печатает сравниваемые пины (`torch==... vllm==...`), а не
  просто число. НОВЫЕ тесты (test_r16_c14_engine_pins.py, РЕАЛЬНЫЙ блок C14):
  оба формата, нераспознанный baseline отвергается, смена torch отвергается
  с именами+версиями, неизменный проходит с печатью пинов, PEP 503
  нормализация имён. Мутация: убрать требование наличия → КРАСНЫЙ.
  Фикстуры адаптированы (маркер извлечения r11 + строка vllm в freeze-фикстурах;
  стаб pytest r12 пишет минимальный валидный junit-отчёт).
* `6efc3cd` **P3**: `scripts/rerun-lmcache-gate.sh [WORK_DIR]` — быстрый
  перезапуск CPU-гейта БЕЗ пересборки wheel (~2-минутные итерации: тесты
  берутся из дерева исходников, не из wheel). Заново применяет патчи к
  свежему checkout пина, прогоняет гейт импортов r15 P1, берёт уже
  собранный wheel (аргумент или новейший /tmp/kvp-lmc-build.* с
  wheels/*.whl) и запускает C8-C12 в закреплённом образе. Условия:
  продакшн-часть патченого дерева (всё кроме tests/ и docs/) должна
  совпадать с деревом, из которого собран wheel (хеш в
  work_dir/rerun-prod-hash.txt → «Need a full build» при несовпадении);
  конечный баннер `NOT production-acceptable: no provenance/identity
  written` (ничего не пишется в live/lmcache-build); та же логика гейтов,
  что в _incontainer_build.sh, через новый общий модуль
  `kvpersist.gate_common` (junit-counts + check-imports), никогда не копия.
  НОВЫЕ тесты (test_r16_rerun_gate.py) гоняют РЕАЛЬНЫЙ скрипт сквозь
  ЛОКАЛЬНЫЙ мини-git-репозиторий с РЕАЛЬНЫМИ git-diff патчами (стаб только
  docker): несовпадение хеша отвергается, отсутствие wheel отвергается,
  баннер печатается, в live/ ничего нет, общий модуль используется везде.
  Финальная приёмка — только полный build-lmcache.sh.
* `58fceaf` **P4**: правки SOURCES.md (5972 также трогает worker_transfer.py /
  attn_window.py / 5 файлов доков; «20 files together» → 22 проверено;
  устаревшая фраза «rebased manually... docs-only changes were not carried
  over» удалена — патч = точный PR-diff). НОВЫЕ тесты (test_r16_shell_gates.py)
  извлекают РЕАЛЬНЫЕ блоки гейта-предка #6608 и отказа при пустом
  GATE_SCOPE из build-lmcache.sh и гоняют их под настоящим bash:
  не-предок PR6608 прерывает сборку, пустой scope отвергается с BUILD
  REFUSED. Мутации `if false;` / `true || {` → КРАСНЫЕ.
Статус аппаратной валидации (новая формулировка, доки x3):
«implemented, unit-tested; hardware validation: C0-C10 passed on the
operator's host (new base 07f1c6bb6), C11 (4136 test fixtures) fixed in r16,
C12-C16 pending».
Полные прогоны обоих репозиториев (раунд 16, финал):
* kv-persistence: 620 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Дополнение раунда 15 (P0′: переход на новую базу пересобранного PR #5972)
* `18e8cde` **P0′** (заменяет P0 раунда 15 выше — тот фикс остаётся в истории
  git как задокументированный ВАРИАНТ ОТКАТА: прежний пин + старый 5972 +
  однострочная правка импорта; история не переписывается). Разработчик PR
  #5972 по нашей просьбе пересобрал PR на `dev`: импорт теперь
  `lmcache.v1.common.custom_types`, а последний коммит («Unify retained-block
  validation across MP store paths») — содержательный новый код (17 файлов,
  +409/−117). Закреплено: база `07f1c6bb6` (на 28 коммитов впереди прежнего
  пина, `dev` #6659), голова PR `bd79d4c6` по точному SHA (правило
  «голова сдвигается» в SOURCES.md). Патч — ТОЧНЫЙ `git diff
  07f1c6bb6..bd79d4c6a` (не правился руками). Проверено на реальном клоне:
  4136 + 5972 применяются чисто на новой базе (20 файлов вместе), гейт
  импортов r15 P1 даёт 0 проблем; новый diff НЕ применяется к прежнему пину.
  Метка версии `0.5.8rc1.post48+kvp.<fp12>` (= `git rev-list --count
  v0.5.8rc1..<pin>` = 48, PEP 440, не ниже реальной базы). #6608 (`bcafb0314`)
  получает тот же гейт-предок, что #6551. НОВЫЕ тесты
  (test_lmcache_pin_consistency.py): один пин везде, 40-hex, метка следует
  базе пина, порядок патчей 4136-до-5972, старый пин/метка исчезли вне
  исторического CHANGELOG. Мутация: вернуть старый SHA в один файл → 2 теста
  КРАСНЫЕ, проверено. **Перепроверка метрик дашбордов (числа по ТЗ):** 117 имён `lmcache_*`
  встречаются в PromQL дашбордов Grafana (число «108» из ревью
  воспроизвести документированным методом не удалось — точный метод подсчёта
  в репо никогда не фиксировали, поэтому оба числа записаны честно). Литералы
  имён в `lmcache/`: 223 (старый пин) против 224 (новая база) — удалено 0,
  добавлен 1 (префикс `lmcache_nixl.` нового NIXL-коннектора, всё равно вне
  упаковки `lmcache/`). Сверка каждого имени дашборда с именами исходников +
  конкатенациями префиксов coordinator: совпало 80 / «пропало» по статическому
  методу 37 — ИДЕНТИЧНО на обеих базах, значит **потеряно миграцией: 0**.
  37 статических «пропаж» — ограничение метода реконструкции имён
  (составные имена/суффиксы), а не регрессия миграции — канонический
  OTel→Prometheus маппинг (METRICS.md, 90 экспортируемых имён) на новой базе
  не изменился. Семейства `vllm:*` в дашбордах исходят от самого vLLM и
  пином LMCache не затрагиваются.
Статус аппаратной валидации (новая формулировка, доки x3):
«implemented, unit-tested; hardware validation: C0-C8 passed on the
operator's host (old base); new base 07f1c6bb6 pending».
Полные прогоны обоих репозиториев (дополнение раунда 15, финал):
* kv-persistence: 598 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Раунд 15 (устаревший импорт патча в C11 + статический гейт импортов + доп. из r14)
* `279f17e` **P0** (лог оператора 19.11 14:57): C0-C8 прошли, затем C11 (сбор
  тестов) умерла rc=2 — `ImportError: cannot import name 'IPCCacheServerKey'
  from 'lmcache.v1.multiprocess.custom_types'`. Патч 5972 делался с головы PR
  (27 окт); апстрим-коммит c4fb19dd перенёс класс в
  `lmcache.v1.common.custom_types` ПОСЛЕ этой головы — патч применялся чисто
  (fuzz), но был СЕМАНТИЧЕСКИ устаревшим («применяется чисто» ≠ «актуально»).
  Фикс: патч ПЕРЕСОБРАН из реального патченого дерева (применить к пину,
  поправить импорт, пересобрать через git diff — хунки не правились руками);
  Patch SHA-256 в SOURCES.md обновлён; тот же устаревший импорт в нашем
  tests/integration/lmc_protocol_client.py исправлен (он молча отключал
  протокольный клиент под try/except). НОВЫЕ тесты (test_r15_patch_porting.py):
  ни одна добавленная (`^+`) строка не несёт устаревший путь, 5972 несёт
  исправленный, каждый Patch SHA-256 в SOURCES.md совпадает с реальными
  байтами. Патч 4136 проаудирован так же: гейт ниже даёт 0 проблем по обоим
  патчам.
* `5c54612` **P1**: статический гейт импортов патченого дерева —
  `implementation/kvpersist/check_patched_imports.py` (эталонная реализация
  из ревью, только AST, без torch/vllm) встроен в build-lmcache.sh сразу
  после патчей/fingerprint, ДО docker-фазы. Scope: файлы, затронутые патчами,
  ОБЪЕДИНЕНИЕ пять файлов CPU-гейта ОБЪЕДИНЕНИЕ их транзитивные
  tests/...-импорты; ПУСТОЙ scope = отказ. Отказы несут `BUILD REFUSED (stale
  patch/test import): <file>:<line>: ...`. НОВЫЕ тесты
  (test_r15_patched_import_gate.py, offline, реальный python, синтетические
  мини-деревья): (а) устаревший импорт → отказ rc=1, (б) исправленный → rc=0,
  (в) fallback в try/except ImportError не флагуется, (г) namespace-пакет
  PEP 420 принимается, (д) _proto_gen / lmcache.lmcache_* в исключениях,
  (е) пустой список файлов → отказ, + проводочный тест (гейт ВЫЗЫВАЕТСЯ в коде
  до docker — упоминания в комментариях не считаются). Проверено на реальном
  клоне пина: 0 проблем на исправленном дереве, ровно 1 на старом 5972.
  Фикстуры (test_docker_stdin_delivery / test_r9_gpu_step_honesty /
  test_r11_gates_real_programs) адаптированы под гейт. Мутация: убрать вызов
  гейта → проводочный тест КРАСНЫЙ.
* `ca1d449` **P2** (доп. из ревью r14): C11/C13 передают
  KVP_LMC_SRC="$LMC_SRC" плагину (защита source-tree была слабее, чем в C8),
  и добавлены недостающие тесты защиты: корень дистрибутива ВНУТРИ
  source_roots (editable-подобный случай) → BUILD REFUSED, через
  assert_installed и через плагин в реальном pytest (exit 4). Мутация: отключить
  защиту → оба КРАСНЫЕ.
Честное ограничение (по ТЗ): гейт ловит устаревшие ПУТИ/ИМЕНА импортов, но не
поведенческие расхождения — падающий патченый тест означает, что портируется
ПАТЧ, а не деселектится/скипается/xfail-ится тест.
Статус аппаратной валидации (новая формулировка, доки x3):
«implemented, unit-tested; hardware validation: C0-C8 passed on the
operator's host; C9-C16 pending».
Полные прогоны обоих репозиториев (раунд 15, финал):
* kv-persistence: 593 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Раунд 14 (ложный отказ C8: раскладка dist-packages против подстроки site-packages)
* `d15c5fc` **P0** (лог оператора 19.11 12:52): C0-C7 прошли (r13 подтверждён на
  железе), затем C8 ЛОЖНО отказал правильно установленному wheel — `BUILD
  REFUSED: lmcache imported from
  /usr/local/lib/python3.12/dist-packages/lmcache/__init__.py, not
  site-packages`. Образ vLLM — Debian/Ubuntu-раскладка, pip ставит в
  dist-packages; скрипты рецептов это знали, сборочные — нет (фикстуры r11
  использовали site-packages-пути, поэтому тесты были зелёными). Фикс:
  критерий по СУТИ — новый общий `assert_installed(mods, source_roots)` в
  scripts/kvp_assert_installed.py находит корень установленного дистрибутива
  lmcache через importlib.metadata и требует, чтобы файл каждого модуля лежал
  ПОД ним (любая раскладка: site-packages, dist-packages, ...), а сам корень не
  находился внутри дерева исходников ($KVP_LMC_SRC/cwd); в отказе — оба пути
  (file, dist root). C8 импортирует общую функцию (PYTHONPATH=$KVP_SCRIPTS), и
  плагин pytest использует ту же (ленивый `import pytest` — модуль работает до
  установки тестовых зависимостей). НОВЫЕ тесты (РЕАЛЬНЫЙ python3 -P +
  РЕАЛЬНЫЙ плагин, фейковые установленные дистрибутивы с dist-info/METADATA):
  (а) site-packages проходит, (б) dist-packages проходит (КРАСНЫЙ до фикса:
  подстрока отказывала — точное воспроизведение железа), (в) дерево исходников
  -> BUILD REFUSED, (г) несовпадение метаданных и модуля -> BUILD REFUSED; то
  же через плагин (exit 4). Тест раскладки r11 дополнен вариантом
  dist-packages. Статические гейты: подстрочного критерия не осталось (grep
  чист), C8 использует общую функцию. Мутация: возврат подстрочной проверки ->
  (б) x3 + линт КРАСНЫЕ, проверено.
Статус аппаратной валидации (сохранён по ТЗ):
«implemented, unit-tested; hardware validation: C0-C7 passed on the
operator's host; C8-C16 pending».
Полные прогоны обоих репозиториев (раунд 14, финал):
* kv-persistence: 581 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Раунд 13 (SIGPIPE в C7: `pip show | head -3` = тихий rc=120)
* `7bda1bc` **P0** (первый запуск на железе, 19.11 11:42): сборка прошла
  C0-C7 (wheel собран, cuda_ops содержит только sm_120, четыре нативных
  расширения, `pip install` прошёл) и затем ТИХО умерла с `docker run rc=120`
  до C8. Причина: `pip show lmcache | head -3` — `head` закрывает пайп,
  `pip show` пишет в закрытый stdout и выходит с 120 (SIGPIPE, `ERROR: Pipe to
  stdout was broken`), под `pipefail` это конец скрипта без сообщения (строка
  БЕЗ `||`). BEFORE-строка к тому же печатала ЛОЖНОЕ `(not installed)` при
  установленном пакете. Тот же класс дефекта, что в r12, но ВНЕ `$(...)` —
  аудит r12 и обходчик сообщений не покрыли обычные конвейеры. Фикс: обе
  строки используют `sed -n '1,3p'` (читает весь вывод), `(not installed)`
  печатается только при реальном отказе `pip show`. НОВЫЕ тесты (реальный
  bash, реальный блок C7 по маркерам, python pip-стаб, пишущий ≥200 КБ и
  выходящий с 120 при закрытом stdout): блок обязан дойти до маркера после C7
  (КРАСНЫЙ до фикса: rc=120 — точное воспроизведение железа), нет ложного
  `(not installed)`, реальный отказ его печатает. Линт приёмки: никаких
  незащищённых `| head` в _incontainer_build.sh. Мутация: возврат `| head -3`
  -> КРАСНЫЙ, проверено. Все конвейеры `| head` / `| grep -q` / `sed q` в
  scripts/ + implementation/ проаудированы — каждое вхождение fail-soft
  (`|| true`), под защитой или ограниченный дамп диагностики.
Статус аппаратной валидации (новая формулировка, доки x3):
«implemented, unit-tested; hardware validation: C0-C7 passed on the
operator's host; C8-C16 pending».
Полные прогоны обоих репозиториев (раунд 13, финал):
* kv-persistence: 570 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Раунд 12 (JSON provenance, тихие падения C6c, порядок C14)
* `125b571` **P0**: литерал brace-дефолта (форма `${VAR:-{}}`) дописывал
  лишнюю `}` к НЕПУСТОМУ JSON — bash закрывает подстановку на первой
  закрывающей фигурной скобке, вторая становилась литералом. Мусор шёл через
  identity.env в provenance.json (`native_extensions` становился невалидным
  JSON), и build-image.sh падал после уже успешной долгой сборки. Фикс:
  дефолт присваивается отдельной строкой (`[ -n "$X" ] || X='{}'`); таких
  литералов не осталось нигде (`grep -rn ':-{' scripts implementation` пуст).
  НОВЫЙ сквозной тест (реальные программы, без ручных подстановок): РЕАЛЬНЫЙ
  `kvpersist.check_wheel_native` на фейковом wheel с 3 нативными .so ->
  блок C6b, извлечённый по маркерам из _incontainer_build.sh -> блок C15 ->
  identity.env -> блок provenance, извлечённый из build-lmcache.sh ->
  `json.loads(provenance.json)`; `native_extensions` обязан быть JSON-объектом
  с ключами lmcache/cuda_ops, lmcache/lmcache_native, lmcache/lmcache_redis.
  Мутация: возврат старой строки -> КРАСНЫЙ (JSONDecodeError: Extra data).
* `6bbc68e` **P1**: тихие падения C6c под `set -Eeuo pipefail`. Поиск `.so`
  шёл через `unzip` (в образе его НЕТ — тихий выход rc=127), а конвейер с
  cuobjdump тихо умирал на пустом выводе. Фикс: `.so` ищется и извлекается
  через python3 zipfile (без unzip), каждый конвейер в подстановках либо
  fail-soft (`|| true`), либо отказывает с ЯВНЫМ `BUILD REFUSED` (пустой
  cuobjdump -> `BUILD REFUSED: cuobjdump produced no architectures` + сырой
  вывод `cuobjdump --list-elf`). НОВЫЕ тесты гоняют РЕАЛЬНЫЙ блок C6c
  (извлечённый по маркерам) под настоящим bash: (а) работает без unzip (был
  тихий rc=127), (б) пустой cuobjdump -> громкий отказ, (в) только sm_89 ->
  `does NOT contain sm_120`. Обходчик приёмки: 7 сценариев отказа РЕАЛЬНОГО
  скрипта целиком — каждый ненулевой выход обязан нести сообщение `BUILD
  REFUSED` / `REGRESSION FAILURE` (он же поймал тихую смерть `nvcc --version |
  tail -1`, исправлена так же). Мутация: возврат unzip-версии C6c -> (а)-(в)
  КРАСНЫЕ.
* `d4cb10a` **P2**: diff engine freeze (C14) перенесён ПОСЛЕ тестовой фазы
  (C9-C13), перед C15 — установка pytest теперь покрывается diff'ом, а гейт
  выполняется и при `KVP_BUILD_SKIP_TESTS=1`. НОВЫЕ тесты гоняют РЕАЛЬНЫЙ
  скрипт целиком с stateful pip-шимом: мутация numpy при установке pytest
  (тесты ВКЛ) или при установке wheel (тесты ВЫКЛ) обязана давать `BUILD
  REFUSED: engine packages changed`; чистый прогон проходит. Мутация: возврат
  C14 перед C9 -> КРАСНЫЙ.
* `19f1ded`: фикстуры r8 подстроены под новый C6c (zipfile-запросы python3 в
  шиме должны исполняться по-настоящему) — фикстуры подстраиваются под
  пайплайн, гейты не ослаблены.
Таблица шаг->тест в docs/lmcache-build.* обновлена (x3); формулировка
«implemented, unit-tested; hardware validation pending» сохранена.
Полные прогоны обоих репозиториев (раунд 12, финал):
* kv-persistence: 566 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Раунд 11 (эталонный пайплайн сборки C0-C16)
* `f6c4312`: `_incontainer_build.sh` перестроен ровно по порядку C0-C16
  (baseline freeze -> apt/git -> гейт тулчейна -> build-зависимости с
  constraints -> ровно один `pip wheel` -> проверки wheel (файлы + ОБА
  нативных расширения + cuobjdump sm_120) -> установка -> import-check из
  site-packages (`python3 -P`) -> diff engine freeze -> минимальные тестовые
  зависимости -> staging без lmcache/ -> staged pytest с новым плагином
  `kvp_assert_installed` -> гейт сводки -> опциональный GPU-шаг ->
  identity.env в одинарных кавычках). Хост: identity.env source строго,
  поля provenance через отдельные строки дефолтов (старый ${VAR:-{}}
  дописывал лишнюю `}`), KVP_TORCH_CUDA_ARCH_LIST пробрасывается.
* `c77dc6f`: фикстуры тестов подстроены под новую фазу (python3-шим срезает
  `-P`, пересоздаёт wheel после rm в C5; тулчейн-стабы; staging-фикстуры;
  экстрактор принимает staged-форму pytest; тест проводки требует staged-форму
  у КАЖДОГО вызова pytest).
* Таблица шаг->тест в docs/lmcache-build.*; мутации (дубль pip wheel / старый
  brace-дефолт / убранный -P) все пойманы.

Прогоны обоих репозиториев (итог раунда 11):
* kv-persistence: 557 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Правки ревью раунда 10 (P0 + P1, по коммиту на пункт; поведенческие тесты вперёд)
* **P0-1** `425adec`: in-container фаза теперь сначала строит wheel (build-зависимости, включая grpcio-tools==1.78.0 -> `pip wheel` -> проверки wheel -> `pip install --no-deps --force-reinstall` -> import-check ИЗ site-packages через `python3 -P` с тремя строками-доказательствами `__file__` -> pytest). Старый порядок импортировал lmcache из PYTHONPATH=$LMC_SRC ДО сборки -- но нативные модули и gRPC-стабы существуют только после неё, из-за чего фаза всегда падала ModuleNotFoundError("lmcache.lmcache_native"). Мутации (import до сборки / PYTHONPATH на исходники) красные.
* **P0-2** `c0efd97`: гейты wheel и образа требуют ОБА нативных расширения (cuda_ops*.so И lmcache_native*.so -- без последнего LMCache уходит в CLI-only режим); опциональные redis/fs пишутся в `native_extensions`; новый маркер KVP_NATIVE_IMPORT=OK в строгом gate build-image.sh. Мутация (только cuda_ops) красная.
* **P1** `425adec`: TORCH_CUDA_ARCH_LIST берётся из переменной проекта KVP_TORCH_CUDA_ARCH_LIST (значение образа -- только fallback, переопределение именуется в логе); identity.env пишет TORCH_CUDA_ARCH_LIST_USED + cuda_ops_archs (cuobjdump --list-elf обязан показать sm_120, иначе BUILD REFUSED); provenance.json берёт torch_cuda_arch_list / cuda_ops_archs / native_extensions из identity.env -- теперь отражает реальную сборку. Мутация (старый fallback на образ) красная.
* **Доп. тесты** `d52ba18`: фикстуры r8/build-image/live-image последовали за новым порядком фаз.

Прогоны обоих репозиториев (итог раунда 10):
* kv-persistence: 549 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Правки ревью раунда 9 (P0 + P1, по коммиту на пункт; поведенческие тесты вперёд)
* **P0** `20fafcf`: в образе vLLM нет git -- in-container фаза падала rc=127 на первой строке (`git config`). Исправлено: `apt-get install git` сразу после обязательного `apt-get update` (громкий отказ, хвост apt виден), строка `git config` safe.directory перенесена ниже установки, после `pip wheel` состав `lmcache/` в wheel сверяется с `git ls-files lmcache` (без .py и .so) -- без git setuptools_scm file-finder молча теряет данные пакета. Таблица аудита внешних программ в коммите. Мутация (git config выше установки) красная.
* **P1-1** `0ad1f2d`: KVP_BUILD_GPU_TESTS=1 теперь передаёт `--gpus all` (массив аргументов) с пре-флайтом nvidia-runtime ДО docker run; GPU-шаг требует torch.cuda.is_available() и разбирает сводку pytest как CPU-гейт -- модульный skip НЕ равен прохождению (skipped != 0 или passed == 0 -> REGRESSION FAILURE со SKIPPED-строками). GPU_REGRESSION_RUN=true только после реального прохождения. Мутации (убрать проверку skip / убрать --gpus) красные.
* **P1-2** `18e0f5d`: provenance.json пишет БУЛЕВО `gpu_regression_run` (поле, обещанное коммитом r8, но не написанное); переменная identity.env переименована в GPU_REGRESSION_RUN (имя, которое читает хост); доки x3 уточняют: tests_skipped = пропуск всей фазы через KVP_BUILD_SKIP_TESTS=1, а не skip-и pytest. Мутация (убрать поле) красная.

### Правки ревью раунда 8 (P0 + P2, по коммиту на пункт; поведенческие тесты вперёд)
* **P0** `03029c2`: GPU-интеграционный набор `test_cache_server.py` (модульный skip без CUDA) больше не блокирует сборку без GPU. Гейт = ПЯТЬ CPU-файлов (skipped==0); GPU-набор запускается только при `KVP_BUILD_GPU_TESTS=1` (--gpus all); `provenance.json` пишет `gpu_regression_run`; отказ гейта перечисляет SKIPPED-строки из `-rs`. Пять файлов проверены: нет skipif/pytest.skip; только `importorskip` в test_vllm_mp_adapter (импортируется в образе vLLM). Мутация (набор обратно в гейт) красная.
* **P2-1** `f78b7d0`: артефакты сборки принадлежали root -- `trap EXIT` возвращает владельца /usr/src/lmcache /kvp-wheels /kvp-work на KVP_HOST_UID:GID (также при ошибке); build-lmcache.sh передаёт host uid/gid. Мутация (убрать trap) красная.
* **P2-2** `29d51cd`: GPU_CC в identity.env совместим с shell (`12.0`, а не кортеж `(12, 0)`, который bash читает как массив). Мутация (кортеж обратно) красная.

### Правки ревью раундов 6-7 (P0 + P1, по коммиту на пункт; поведенческие тесты вперёд)
* **A-1/A-3/A-4** `68f68c9`: in-container фаза сборки перенесена из stdin-heredoc в смонтированный ФАЙЛ (`scripts/_incontainer_build.sh`). Воспроизведено на машине оператора: `docker run` БЕЗ `-i` оставил `bash -s` ПУСТЫЙ stdin, фаза молча исчезла, скрипт падал 'no wheel produced' в пустой вывод. Заглушка docker теперь ведёт себя как настоящая (stdin только с `-i`; смонтированный файл в argv — это и есть запуск), отказ несёт docker rc + рабочий каталог + последние 20 строк pytest-report.txt. Прочие stdin-heredoc места проверены (build-image.sh исправлен в A-2; пробы используют -c).
* **A-2** `9223306` + `6ebaa04`: verify-фаза build-image.sh — смонтированный ФАЙЛ (`scripts/_verify_image.py`) плюс СТРОГИЙ gate маркеров: `KVP_CUDA_OPS_IMPORT=OK`, `KVP_PY_IMPORTS=OK`, `KVP_IDENTITY_FILE=OK`, `KVP_GPU_SMOKE_OK_COUNT == KVP_GPU_COUNT > 0` ДО записи чего-либо -- старый heredoc давал пустой verify.txt и ЛОЖНЫЙ успех.
* **B-1** `8937148`: ОДИН pin-файл `vendor/pinned-base-image.env` для базового образа vLLM. Чистый клон собирается БЕЗ `export IMAGE`; `KVP_BASE_IMAGE` переопределяет; `IMAGE`/`KVP_LMC_IMAGE` в сборочных скриптах игнорируются (с предупреждением); плавающий тег отклоняется, отказ называет pin-файл.
* **B-2** `049a90f` (kv) + `71dc08c` (recipes): ID custom-образа записывается в `live/image` ТОЛЬКО после verify-gate; `start-cache.sh`/`start-all.sh` и лаунчеры рецептов с KV_PERSIST=1 берут его при пустом `IMAGE`; упавший smoke никогда не создаёт и не перезаписывает запись.
* **B-3** `4db5b3f`: консистентность пина -- дайджест живёт ТОЛЬКО в pin-файле (хардкод в заголовке build-image.sh убран); кросс-тест сверяет `tp4.env.example` рецепта mimo-v2.6-pro-hotsplit (без checkout recipes — skip с явной причиной).
* **B-4** `10df544`: предполётные проверки в build-lmcache.sh, каждая с одной строкой 'что делать' (нет docker / демон недоступен / размер pull образа / место на диске), и раздел «С нуля за три команды» в README EN/RU/ZH с разграничением базового и custom образов.
* **Доп. тесты** `3cac6c9`: статические wiring-тесты последовали за переносом в A-1; формулировка быстрого старта сохраняет порядок «мониторинг раньше кэша».

Прогоны обоих репозиториев (итог раундов 6-7):
* kv-persistence: 533 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Правки ревью раунда 5 (P1, по коммиту на пункт; поведенческие тесты вперёд)
* **P1-1** `df8f096`: reset отказывал на реальном кэше без маркера (allowlist не знал артефакты, которые ЛИЧНО пишет адаптер LMCache). Источник правды `fs_l2_adapter.py` / `key_codec.py`: `encode_fs_filename` -> `<имя>.data` на каждый ObjectKey, временные файлы получают суффикс `final.with_suffix('.tmp')` либо кладутся в подкаталог `relative_tmp_dir` из конфига. В allowlist добавлены `*.data`, `*.tmp` (узкие маски, без `*`) и имя каталога `relative_tmp_dir`, читаемое из `l2-adapter.rendered.json` самого кэша (без ключа ничего не добавляется). Поведенческие тесты (test_reset_legacy_cache_allowlist.py): старый кэш без маркера проходит БЕЗ ручного touch; один посторонний файл — отказ, где в перечне неизвестных ровно этот файл; неконфигурированный tmp-каталог остаётся чужим. Мутация (убрать `*.data`) красная.
* **P1-2** `c5d75f0`: маркер корня кэша задокументирован для операторов x3 (EN/RU/ZH, раздел reset в docs/lmcache-build.*): зачем маркер и что его создаёт start-cache.sh при инициализации; точная процедура для старого кэша `ls -A <KVP_CACHE_ROOT>` + `touch <KVP_CACHE_ROOT>/.kvp-cache-root-marker` (буквально совпадает с подсказкой в отказе reset, куда добавлен `ls -A`); безусловные отказы ($HOME / /root / /home/<user>, предки checkout, `.git` на глубине ≤ 2). Формулировка «implemented, unit-tested; hardware validation pending» сохранена. Статические словесные тесты не писались — по правилу раунда.

Прогоны обоих репозиториев (итог раунда 5):
* kv-persistence: 515 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Правки ревью раунда 4 (P0/P1, по коммиту на пункт; поведенческие тесты вперёд)
* **P0-1** `0d20ce6`: суффикс CUDA dev-пакетов считался неверно (`%.*` отвечал `13` для torch CUDA `13.0` → несуществующий `cuda-cudart-dev-13`, сборка всегда падала «BUILD REFUSED»); не было `apt-get update` (образ vLLM чистит apt-списки); ошибки apt прятались за `>/dev/null 2>&1`. Исправлено: `scripts/_cuda_dev_suffix.sh` (единый источник, `major-minor`: 13.0→13-0 / 13.0.2→13-0 / 12.8→12-8), `apt-get update -y` перед циклом установки, последние 20 строк вывода apt печатаются при любом отказе. Также P1: пустая summary-строка pytest теперь — явный отказ «pytest summary not found». Поведенческие тесты (test_build_lmcache_cuda_dev.py) извлекают настоящий in-container apt-блок и гоняют его с заглушкой apt-get; суффикс проверяется прямым вызовом helper-скрипта; мутация `%.*` — красная.
* **P0-2** `2493925`: регрессия безопасности reset — r3 P1-2 убрала защиту вместе с блокировками /mnt//home, поэтому явный `KVP_CACHE_ROOT=$HOME` (опечатка в cache.env) стирал домашний каталог. Исправлено: явный корень кэша обязан доказать свою принадлежность — маркер `.kvp-cache-root-marker` (создаётся start-cache.sh при инициализации кэша) либо ВСЕ записи верхнего уровня из известного набора LMCache/kvpersist (lmcache-server.log/.pid, l2-adapter.rendered.json, engine-identity.env, index.db, crash-*, objects/, kvp-*, *.bin, *.bin.tmp.<uuid>, .kvp-active-writes). Всегда отказ: $HOME (текущая и любая /home/<user> / /root), любой предок каталога checkout, любой каталог с `.git` на вложенности ≤ 2. Отказ перечисляет неизвестные записи + подсказку осмотреть и проставить маркер вручную. /mnt/<x>/<dir> с маркером или известным набором принимается как раньше. Поведенческие тесты (6 случаев в test_reset_explicit_root_policy.py) — красные до фикса, мутация проверки известного набора — красная.
* **P0-3** `58a1866`: `--deep` существовал в check_image_identity.py, но verify-image-custom.sh и `_kvp_require_custom_image` вызывали модуль БЕЗ `--deep`, и каждый реальный запуск оставался LABEL-only. Исправлено: оба входа выполняют deep-путь ПО УМОЛЧАНИЮ; `KVP_VERIFY_SHALLOW=1` — единственная явная лазейка для тестовых стендов (в одном ряду с `KVP_SKIP_IMAGE_CHECK`). Deep теперь сверяет реально установленную версию (`importlib.metadata.version('lmcache')` == wheel_version из identity), отказывает любой identity без `+kvp.`-суффикса и при недоступности метаданных. Поведенческие тесты (test_deep_default_wiring.py) гоняют настоящий verify-image-custom.sh со стабом docker (labels верные, `cat` identity падает → rc ≠ 0); мутация удаления `--deep` — красная.
* **Мелочь** `7ae5464`: exec-бит на `scripts/_cuda_dev_suffix.sh` (100755).

### Правки ревью раунда 3 (P0/P1, по коммиту на пункт; поведенческие тесты вперёд)
* **P0-1** `5f45986`: build-image.sh падал при ПЕРВОМ запуске — heredoc Dockerfile использовал `$PR4136_SHA`/`$PR5972_SHA`/`$WHEEL_SHA_ID` ДО их вычисления (unbound variable). Исправлено: все три sha256 вычисляются сразу после `cp $WHEEL`, дубль блока ниже удалён. Поведенческий тест (tests/unit/test_build_image_runs.py) прогоняет сам скрипт со стабом docker и tmp-фикстурами (KVP_PATCH_DIR/KVP_BUILD_DIR/KVP_IDENTITY_OUT — без записи в репозиторный live/); мутационный детектор восстанавливает старый порядок и проверяет точный обвал.
* **P0-2** `b1afd2e`: git dubious-ownership + фиксация версии wheel. Контейнер работает от root поверх примонтированного /usr/src/lmcache с host-uid; git отказывал читать репозиторий, а у setuptools_scm не был задан fallback_version → wheel собирался как 0.0.0 / 0.1.dev. Исправлено: `git config --global --add safe.directory /usr/src/lmcache` перед `pip wheel`; `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=0.5.7.dev428+kvp.<12 hex PATCHED_STATE>`; в kvpersist/lmc_identity.py добавлена `compare_wheel_version()` (единый источник правды: «голая» версия upstream-nightly отклоняется даже при точном совпадении). Provenance + запечённый identity-файл несут wheel_version. Поведенческий тест dubious-ownership SKIPается в этой песочнице (chown молча no-op на tmpfs) — на стенде оператора тест отработает и докажет баг.
* **P0-3** `390b6a7`: установка CUDA dev-заголовков в контейнере сборки. Runtime-образ vLLM содержит nvcc + cuda-cudart, но НЕ cuda-cudart-dev / libcusparse-dev / libcusolver-dev / libcublas-dev (собственный Dockerfile vLLM удаляет их после своей source-build-стадии LMCache). `apt-get install -y --no-install-recommends` четырёх пакетов с суффиксом из `torch.version.cuda` (13.0 → 13-0); отсутствие пакета обрывает сборку С НАЗВАНИЕМ пакета (без `|| true` — это именно то, что давало wheel без cuda_ops). `export CUDA_HOME=/usr/local/cuda`. Dockerfile производного образа остаётся без dev-пакетов (тест проверяет отсутствие строк apt-get/-dev).
* **P1-1** `d7275a5`: код возврата pytest перехватывается (больше нет `|| true`), число пропусков берётся из ИТОГОВОЙ строки pytest (никогда — из счётчиков строк «SKIPPED»).
* **P1-2** `12ccd8e`: политика путей сброса — явный `KVP_CACHE_ROOT` побеждает мягкие запреты `/mnt`/`/home` (реальный NVMe монтируется в /mnt), строгая граница `/` в префиксном сравнении (корень `…/l2` больше не подхватывает соседний `…/l2-old`), переменная `KVP_LIVE_DIR` для стейджинга тестов (никогда — в репозиторный live/).
* **P1-3** `a2deeaf`: глубокая проверка образа за пределами LABEL (LABEL подделывается одной строкой): чтение запечённого `/etc/kvp-lmcache-identity.json` через `docker run --entrypoint cat`, сверка sha патчей с `vendor/patches/lmcache` и проба `find_spec('lmcache.cuda_ops')` через `docker run --entrypoint python3` — любое расхождение прерывает запуск. Флаг CLI `--deep`, связка через `verify-image-custom.sh`.
* **P1-4** `доки`: честные формулировки «implemented, unit-tested; hardware validation pending» вместо непроверенных «verified/works» про фикс гонки, кастомную сборку и нативное расширение (EN + RU + ZH); новый раздел 5b «Implementation stages» фиксирует 6-шаговую цепочку аппаратной валидации — переименовывать в «validated on hardware» только после прохождения.

### Правки ревью раунда 2 (P0/P1, по коммиту на пункт)
* **P1-1** `87b1083`: восстановлены пины `nixl==1.5.2`/`openai`/`zstandard`/`lz4`,
  потерянные при перезаписи r1 (без nixl не стартовал L2-адаптер, без openai —
  CLI); тест пинов перенацелен под новый контракт.
* **P1-2** `95fa1a8`: идентичность сборки запечена в образ
  (`/etc/kvp-lmcache-identity.json`, base + SHA-256 патчей + SHA-256 wheel) и
  сверяется гейтом (MISMATCH → код 4); `PATCHED_STATE` хэширует diff;
  `pytest -rs` со skip=FAIL; честные доки ×3.
* **P0-2** `03fe204`: wheel без `cuda_ops*.so` ОТКАЗЫВАЕТСЯ (молчаливый
  fallback без nvcc и был причиной cudaMemcpy error 1): nvcc-preflight,
  `BUILD_WITH_CUDA=1 TORCH_CUDA_ARCH_LIST=12.0`, проверка .so в wheel,
  честный GPU-смоук нативного ядра на каждой GPU (`--gpus all`), предупреждение
  о fallback в логе сервера.
* **P0-3** `4fdff85`/`1678964`: reset-persistent-cache.sh переписан —
  диагностический лог/pid/rendered-json/crash-* сохраняются (архив в live/),
  симлинк-корни реально очищаются (resolved-path обход + post-check),
  подтверждение «выделенный каталог» (resolved-сравнение, симлинк корня
  работает), отказ сетевых ФС, детект живого host-MP (stale-pid игнорируются),
  полный отчёт по 7.4. 9 тестов.
* **P0-1** `a259929`: мандат custom-образа на старте — стоковые vLLM-образы
  отвергаются start-cache.sh (дефолт `KVP_LAUNCH_MODE=docker`) и обоими
  рецептурными лаунчерами при KV_PERSIST=1 (метки образа vs SHA-256 патчей до
  загрузки модели, `verify-image-custom.sh`); пин по ID образа
  (`sha256:<64hex>`) принят для локально собранных производных. 13 тестов.

### Интеграция LMCache: два обязательных апстрим-фикса + сброс кэша
* Создан `vendor/patches/lmcache/`: `4136-mp-retrieve-abort.patch` (гонка
  retrieve/abort: явный жизненный цикл `LOADING`, передача подтверждённых
  планировщиком прерываний воркерам, подавление невалидных store-завершений,
  дедупликация ID прерываний), `5972-incomplete-kv-objects.patch` (проверка
  доступности блоков после downsampling; неполные объекты с `null_block_id`
  отклоняются до персистентности, независимо полные группы сохраняются), а
  также `SOURCES.md` с закреплённым базовым коммитом, ревизиями каждого PR и
  контрольными суммами SHA-256. Подтверждено, что #6551 уже слит в базу
  (проверка `git merge-base --is-ancestor`, локального дубля нет).
* `scripts/build-lmcache.sh` (новый): воспроизводимая кастомная сборка LMCache
  внутри закреплённого образа vLLM; проверка ревизии базы и наследования #6551;
  детерминированное применение обоих патчей (громкий отказ, если патч не
  применился или уже применён); прогоны узких регрессионных наборов #4136 и
  #5972; сборка wheel с `--no-build-isolation` против закреплённого PyTorch;
  запись полной идентичности исходников + SHA-256 wheel в
  `live/lmcache-build/provenance.json`.
* `scripts/build-image.sh` (новый): сборка производного образа от digest
  закреплённого vLLM с установкой ровно кастомного wheel LMCache; побитовое
  сохранение vLLM/PyTorch/CUDA/Python; запись `live/image-identity.env` с
  итоговым digest и идентичностью рантайма; этот единый образ обслуживает и
  общий MP-сервер кэша, и каждый воркер модели.
* `scripts/reset-persistent-cache.sh` (новый): ЕДИНСТВЕННАЯ деструктивная
  операция (разовая миграция). Требует `KVP_RESET_CONFIRM=FULL-RESET`;
  отклоняет небезопасные пути (`/`, home, модели, репозитории, убегающие
  симлинки); принудительно останавливает/проверяет остановку сервисов; пишет
  `live/cache-reset-report-<UTC>.txt`. Никогда не вызывается из
  start/stop/restart.
* `docs/lmcache-build.{md,ru.md,zh.md}` (новый): идентичность сборки LMCache,
  развёртывание кастомного образа, L1/L2 + идентичность кэша + диагностика
  STORE/RETRIEVE («STORE_END — не доказательство персистентной записи»),
  процедура безопасного сброса кэша, диагностика промахов и явная процедура
  будущей миграции для удаления двух downstream-патчей после слияния PR
  #4136/#5972 в апстрим.

### Правки ревью раунда 80 (честные метрики, дрейф состояния, жизненный цикл)
* метрика `ask()` переименована `ttft_s` -> `request_latency_s` (non-stream-проба
  меряет ПОЛНУЮ латентность запроса, а не Time-To-First-Token -- честное имя);
* `start-all.sh` экспортирует ЕДИНЫЙ канонический `LIVE_DIR=$ROOT/live` (дефолт
  рецепта `$PWD/live` дрейфовал при запуске из другого каталога); запись
  `live/image` идёт ПОСЛЕ проверки digest (невалидный IMAGE туда не попадает);
* `stop-all.sh` останавливает и observability-стек; `stop-engine.sh` делает
  graceful `docker stop --time 30` перед `rm -f`;
* рецептный лаунчер запускает общие pre-flights при прямом запуске (с защитой
  от повтора через KVP_PREFLIGHTS_DONE) -- один уровень диагностики;
* чистка доков: все ссылки на удалённый kv-лаунчер заменены, имя контейнера и
  дата аппаратной приёмки (2026-11-10) исправлены;
* ослабленные тесты восстановлены + регрессия: start-all.sh из постороннего
  каталога должен писать всё состояние в repo live/.

## Раунд 80b (legacy kv-лаунчер удалён; все проверки СОХРАНЕНЫ)
* **`run-vllm-with-persist.sh` УДАЛЁН** -- у `start-all.sh` теперь ОДИН путь
  движка (`KVP_ENGINE=recipe`). `KVP_ENGINE=kv` честно отказывает с
  понятной ошибкой (ничего не потеряно: общие проверки живут в `env.sh` /
  `hotsplit_env.sh`).
* **Все проверки старого лаунчера проаудированы и СОХРАНЕНЫ** -- три
  pre-flight-проверки (доступность TCP, совпадение L2-кодека, совпадение
  версии lmcache), диагностика кэш-сервера при смерти движка, GPU-чек +
  PID-чек кэш-сервера, запись `engine-identity.env` и consent-гейт
  переопределения namespace (раунд 72) перенесены в рецептный лаунчер /
  общий `_kvp_pre_flights`. Они жили ТОЛЬКО в удалённом скрипте (класс
  дефекта раунда 76).
* 22 kv + 5 recipes тестовых файлов мигрированы: текстовые проверки
  читают ОБЪЕДИНЕНИЕ контракта (`_targets.contract_text()` /
  `_contract.py`), поведенческие управляют рецептным лаунчером.
  Parity-тест (два лаунчера) стал тестом docker-контракта движка (один).
* kv 378+20, recipes 388+5 -- всё зелёное.

## Раунд 80 (дефолтный движок = recipe, валидация на железе ПРОЙДЕНА)
* **Прогон на железе ПРОЙДЕН целиком 2026-11-10** (`test-recovery.sh`
  `VERDICT: PASS`, exit 0): движок same-UID (1000:1000) на рецептном пути,
  сценарий A (рестарт только движка) — memory/L1 hit, `cached_tokens: 2560`
  и TTFT 2.44 с против 11.49 с холодного; сценарий B (полный рестарт) —
  дисковое восстановление с L2-событиями чтения и 411 МБ, прочитанными
  процессом кэш-сервера.
* **`KVP_ENGINE=recipe` теперь ДЕФОЛТ** (`start-all.sh`). Старый kv-лаунчер
  остаётся как явный legacy-путь `KVP_ENGINE=kv` до вычистки раунда 80;
  все общие проверки живут в `env.sh` и работают на обоих путях.

## Раунд 79 (регрессия same-UID на рецептном пути, fail-fast + диагностика в recovery-тесте)
* **Корень повторяющегося таймаута `register_kv_caches` (300 с) подтверждён на
  железе** (ревьюер + драйверный лог): делегированный рецептный путь запускал
  движок как ROOT, а кэш-сервер на хосте работал как обычный пользователь --
  именно этот разрыв UID даёт `CUDA_ERROR_MAP_FAILED` ("Failed to
  open/create interprocess memory file / remote IPC semaphore pool" = EACCES
  на root-owned `cuda.shm.*` в /dev/shm). Контракт same-UID из раундов 45h/48
  (kv-лаунчер) ПОТЕРЯЛСЯ при делегации -- тот же класс дефекта, что в раундах
  76/77/78.
* `run-tp4-hotsplit.sh` теперь запускает движок как `$(id -u):$(id -g)` ПО
  УМОЛЧАНИЮ (`KVP_SAME_UID=1`, отказ -- `KVP_SAME_UID=0`) с HOME / HF / XDG /
  TRITON-кэшами на `/tmp` -- идентично `run-vllm-with-persist.sh`.
* Ожидание API в рецептном пути теперь **fail-fast** по свежему `Error handling
  operation` в логе кэш-сервера (причина печатается; метка берётся ДО `docker
  run`, чтобы поймать и сбой в первые секунды) вместо ожидания 300-секундного
  таймаута движка.
* `test-recovery.sh`: `api_ready` так же делает fail-fast И печатает
  `docker logs` при смерти контейнера (без ручной диагностики); сценарий B
  прогоняет CUDA IPC pre-flight ДО `docker start` (то самое место, где B упал);
  все чтения лога сервера -- через `grep -a` (NUL-байты заставляют обычный grep
  молча пропускать совпадения).
* `start-all.sh` передаёт флаг same-UID общим pre-flights (они должны проверять
  ту границу, которой движок реально пользуется).
* Регрессионные тесты: `tests/unit/test_round79_recovery_failfast.py` (kv,
  5 тестов) + `tests/test_round79_same_uid.py` (recipes, 3 теста) -- красные
  на старом коде. Allowlist parity обновлён: `--user` и маунт `/tmp`-кэша
  теперь ОБЩИЕ токены (оба пути same-UID), а не различия.



