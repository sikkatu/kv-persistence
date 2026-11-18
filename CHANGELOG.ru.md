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



