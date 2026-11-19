# Кастомная сборка LMCache: патчи, сборка, идентичность, сброс, будущая миграция

Документ описывает кастомное развёртывание LMCache, которое обслуживает общий
MP-сервер кэша и каждый vLLM-воркер модели. Английская версия:
`lmcache-build.md`; китайская — `lmcache-build.zh.md`.

## 1. Идентичность сборки LMCache

| Пункт | Значение |
| --- | --- |
| Апстрим-проект | https://github.com/LMCache/LMCache |
| Закреплённый исходный коммит (база) | `07f1c6bb642f4772606a36eab730ae009ddce08f` |
| Идентичность базового релиза | `0.5.8rc1.post48` (= `git rev-list --count v0.5.8rc1..<pin>` = 48) |
| Коммит слияния PR #6551 (в базе) | `61faa902a81462177d40d74f8c64bb9df02163c4` |
| Исходная ревизия PR #4136 (патч) | `b10588bd46288192e01ab06da3c3fa923ad0401a` |
| Исходная ревизия PR #5972 (патч) | `bd79d4c6acf6096061b5092517698239440cb93b` |
| Каталог патчей | `vendor/patches/lmcache/` |
| Детали и контрольные суммы патчей | `vendor/patches/lmcache/SOURCES.md` |
**Правило портирования (r15):** патч, взятый с головы PR, ОБЯЗАН проходить
статический гейт импортов (`implementation/kvpersist/check_patched_imports.py`,
запускается `build-lmcache.sh` сразу после патчей, до docker-фазы) на
закреплённой базе. **«Применяется чисто» ≠ «семантически актуально»** —
апстримовские переносы/переименования (как перенос `IPCCacheServerKey` в
`lmcache.v1.common.custom_types`) переживают fuzz `git apply` и падают только
на импорте внутри 20-минутной сборки (лог оператора 19.11 14:57, C11). После
каждой смены пина заново прогонять гейт и портировать патч на новую базу
(править устаревшие имена в патченом дереве и пересобрать через `git diff` —
не редактировать хунки руками); никогда не ослаблять и не пропускать гейт,
чтобы протащить устаревший патч.

Изменяемый тег `nightly` в продакшене никогда не используется: при каждой
сборке проверяется точный базовый коммит. `scripts/build-lmcache.sh`
проверяет ревизию базы и то, что #6551 является её предком, детерминированно
применяет оба патча (громко завершается, если патч не применился или уже
применён), гоняет узкие регрессионные наборы тестов для #4136 и #5972, собирает

### Эталонный пайплайн сборки C0-C16: шаг -> закрывающий тест (r11)

| Шаг | Гейт | Тест с настоящей программой |
| --- | --- | --- |
| импорты патчей | статический гейт импортов патченого дерева (до docker) | test_stale_import_refused_with_message / test_fixed_import_passes / test_fallback_import_not_flagged / test_namespace_package_accepted / test_exempt_modules_and_proto_gen / test_empty_file_list_refused / test_gate_wired_in_build_before_docker (test_r15_patched_import_gate.py) + test_no_stale_ipccacheserverkey_import_in_patches / test_5972_carries_the_fixed_import / test_sources_md_sha256_matches_patch_files (test_r15_patch_porting.py) + test_patch_test_fixtures_carry_the_adapted_attributes / test_no_pending_store_left_in_the_patch (test_r16_patch4136_fixtures.py, r16 P0: фикстуры 4136) + test_pr6608_ancestor_gate_dies_when_not_ancestor / test_empty_gate_scope_refused (test_r16_shell_gates.py, r16 P4.2) |
| быстрый перезапуск | rerun-lmcache-gate.sh: сверка production-хеша + баннер NOT-production (без записи в live/) | test_production_hash_mismatch_refused / test_no_wheel_refused / test_success_path_prints_not_production_banner / test_nothing_written_to_live / test_shared_gate_module_used_everywhere (test_r16_rerun_gate.py, r16 P3) |
| C0 | пролог: trap EXIT chown, baseline pip freeze | test_chown_runs_even_on_pytest_failure (test_r8_gpu_gate_and_artifacts.py) |
| C1 | apt update -> git + cuda dev pkgs (громкий отказ) | test_git_is_installed_before_first_use / test_missing_git_package_refuses_loudly (test_r9_git_install.py, реальный bash+лог apt) |
| C2 | git safe.directory после установки | тот же r9-тест (порядок из логов apt/git) |
| C3 | гейт nvcc + g++ + ninja, оверрайд архитект | test_project_arch_override_wins_over_image (test_r10_build_order.py) |
| C4 | build-зависимости с constraints движка, без тихого fallback | test_engine_freeze_diff_refuses_changed_torch (та же логика constraints) |
| C5/C16 | ровно один `pip wheel` | test_exactly_one_pip_wheel_in_the_script + test_pip_wheel_runs_exactly_once (реальный прогон, пишущий pip shim) |
| C6a | состав wheel против git ls-files | покрыто фикстурами реального прогона (пропавший файл = отказ) |
| C6b | оба нативных расширения | test_wheel_without_lmcache_native_refused (test_r10_build_order.py) + test_check_wheel_native.py (реальный zipfile+CLI) + test_c6b_identity_provenance_chain_delivers_valid_json (r12: РЕАЛЬНЫЙ check_wheel_native -> C6b -> C15 -> provenance, json.loads) |
| C6c | cuobjdump sm_120, без тихих падений | test_c6c_works_without_unzip / test_c6c_empty_cuobjdump_refuses_loudly / test_c6c_sm89_only_refuses_loudly (r12, РЕАЛЬНЫЙ bash на извлечённом блоке C6c) + test_every_refusal_scenario_prints_a_message (обходчик приёмки) + путь отказа по архитектурам в фикстурах test_r10_build_order.py |
| C7 | установка wheel + pip show до/после (без SIGPIPE) | test_c7_reaches_after_marker_despite_big_pip_show / test_c7_before_line_no_false_not_installed / test_c7_before_line_reports_missing_install (r13, РЕАЛЬНЫЙ bash, pip-стаб 200 КБ) + test_no_head_pipeline_outside_fail_soft_substitutions (lint) |
| C8 | import-check из установленного дистрибутива (учитывает dist-packages) | test_layout_run_sees_installed_not_source / test_plugin_refuses_source_tree_with_exit4 (РЕАЛЬНЫЙ pytest + плагин) + test_installed_dist_packages_layout_passes / test_source_tree_refused / test_metadata_and_module_mismatch_refused / test_plugin_passes_dist_packages_layout (r14: общий assert_installed, обе раскладки) |
| C14 | diff engine freeze (после тестов, перед C15; выполняется и при KVP_BUILD_SKIP_TESTS=1; требует torch+vllm в baseline, разбирает `name==ver` + `name @ url`) | test_engine_freeze_diff_refuses_changed_torch (реальный python на freeze-файлах) + test_c14_runs_after_pytest_install_and_detects_mutation / test_c14_runs_even_when_tests_are_skipped / test_c14_positive_control_no_mutation (r12, РЕАЛЬНЫЙ весь скрипт, stateful pip shim) + test_both_freeze_formats_are_parsed / test_unrecognised_baseline_is_refused / test_changed_torch_version_refused_with_names_and_versions / test_unchanged_passes_and_prints_pins / test_pep503_name_normalisation (test_r16_c14_engine_pins.py, r16 P2) |
| C9 | минимальные тестовые зависимости (только pytest) | проводка в фикстурах реального прогона |
| C10 | staging без lmcache/ | test_layout_run_sees_installed_not_source (копирование staging проверяется) |
| C11 | staged pytest + kvp_assert_installed (KVP_LMC_SRC передаётся, защита source-tree активна) | test_script_runs_the_exact_command (каждый вызов в staged-форме) + тесты плагина + test_distribution_inside_source_roots_refused / test_plugin_refuses_distribution_inside_source_roots (r15 P2.2) + 5 патченых тестов, исправленных в r16 P0 (адаптация фикстур: test_patch_test_fixtures_carry_the_adapted_attributes) |
| C12 | гейт сводки (rc==0, junit: failures==0, errors==0, skipped==0, passed>0) | test_green_run_passes_with_log_cli / test_green_run_passes_without_log_cli / test_skip_is_refused_and_lists_skipped_rows / test_failure_is_refused / test_missing_junit_report_is_refused / test_no_text_summary_grep_left_in_the_gate (test_r16_c12_junit_gate.py, r16 P1: РЕАЛЬНЫЙ pytest в обеих конфигурациях log_cli) |
| C13 | GPU-шаг (skip != 0 => отказ) | test_gpu_step_skip_is_not_success (test_r9_gpu_step_honesty.py) |
| C15 | identity.env в одинарных кавычках, строгий source | test_identity_env_sources_cleanly_under_strict_bash + test_provenance_json_fields (РЕАЛЬНЫЙ bash/json) + test_c6b_identity_provenance_chain_delivers_valid_json (r12 сквозной) |
| хост | поля provenance из identity.env | test_provenance_json_fields (реальный прогон build-lmcache.sh) + test_c6b_identity_provenance_chain_delivers_valid_json (r12: json.loads поле native_extensions) |

Порядок шагов сборки (r10): apt/git -> build-зависимости (включая
`grpcio-tools==1.78.0` для генератора proto-стабов) -> `pip wheel` ->
проверки состава wheel (git ls-files + оба нативных расширения) ->
`pip install --no-deps --force-reinstall` -> import-check ИЗ site-packages
(`python3 -P`, три строки-доказательства `__file__`) -> pytest (пять
CPU-файлов, skipped == 0) -> identity/provenance. Список архитектур берётся
из `KVP_TORCH_CUDA_ARCH_LIST` (по умолчанию 12.0; значение образа -- только
fallback), и собранный `cuda_ops*.so` обязан содержать sm_120 (cuobjdump).
`provenance.json` пишет `torch_cuda_arch_list`, `cuda_ops_archs` и
`native_extensions` из identity.env -- он отражает реальную сборку.

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

wheel с `--no-build-isolation` против установленного в закреплённом образе
PyTorch и записывает `live/lmcache-build/provenance.json` с полной идентичностью

`tests_skipped` в `provenance.json` означает, что фаза тестов ЦЕЛИКОМ
пропущена через `KVP_BUILD_SKIP_TESTS=1` -- это НЕ про отдельные skip-и
pytest (их гейты отказывают). `gpu_regression_run` -- булево свидетельство
того, что GPU-шаг регрессии реально выполнился и прошёл (модульный
skip не считается выполнением).
исходников и SHA-256 wheel.

Оба патча малы, не пересекаются по файлам и независимо удаляемы:

* `4136-mp-retrieve-abort.patch` — только слой интеграции с vLLM MP
  (явный жизненный цикл retrieve `LOADING`, передача подтверждённых
  планировщиком прерываний воркерам, подавление невалидных store-завершений,
  дедупликация ID прерываний);
* `5972-incomplete-kv-objects.patch` — только слой MP-движка кэша
  (проверка доступности блоков после downsampling окна; неполные объекты с
  оставшимся `null_block_id` отклоняются до персистентности; независимо
  полные группы сохраняются).

## 2. Развёртывание

`scripts/build-image.sh` записывает идентичность результирующего образа в
`live/image-identity.env` и запекает её в образ как
`/etc/kvp-lmcache-identity.json` (базовый коммит, SHA-256 патчей, SHA-256
wheel) — по одной только версии wheel (`0.5.8rc1.post48`) нашу сборку от
апстримного nightly не отличить (`no-local-version`).

**Статус развёртывания (r2 P0-1 — активно):** кэш-сервер теперь по умолчанию в
режиме `KVP_LAUNCH_MODE=docker`, а оба рецептурных лаунчера отказываются
запускаться, если образ не несёт custom-идентичность LMCache
(`verify-image-custom.sh` / `kvpersist.check_image_identity` сверяют метки
`org.kvp.lmc-*` с SHA-256 файлов из `vendor/patches/lmcache/*.patch` до
загрузки модели). Стоковый vllm-openai стартует только с явным
`KVP_LAUNCH_MODE=host` + `KVP_SKIP_IMAGE_CHECK=1` (opt-out тестовых харнесов) и
на страх и риск оператора. `verify-engine-identity.sh` дополнительно сверяет
`/etc/kvp-lmcache-identity.json` работающего контейнера (MISMATCH, код 4).

Этот образ обязаны использовать:

* общий MP-сервер кэша (`scripts/start-cache.sh`),
* каждый vLLM-воркер модели (лаунчеры рецептов).

Запуск обязан завершаться ошибкой, если воркер и MP-сервер расходятся в
идентичности сборки LMCache. Pre-flight identity-гейты
(`verify-engine-identity.sh`, `_kvp_pre_flights`) выполняются до загрузки модели.

Выбор коннектора: используемая версия vLLM требует ЯВНОГО выбора внешнего
`LMCacheMPConnector` через `--kv-transfer-config` (не рассчитывайте на
автоопределение).

## 3. Персистентный кэш: L1/L2, идентичность, диагностика

* L1 = DRAM хоста (`LMCACHE_L1_SIZE_GB=64`; проверяйте фактическую опцию
  сервера в `lmcache-server.log` — не предполагайте, что переменная
  потребляется).
* L2 = постоянный уровень на NVMe (`KVP_CACHE_ROOT=/var/lib/kv-persistence/l2`
  по умолчанию; обязательно локальный NVMe-путь).
* Кодек — только без потерь: `zstd` (по умолчанию), `lz4`, `none`.
* Идентичность кэша (namespace, идентичность модели, KV dtype `bfloat16`,
  формат кэша, конфигурация object-group, идентичность бэкенда) проверяется
  на каждом воркере в preflight; любое расхождение останавливает запуск.

**Событие `STORE_END` — не доказательство персистентной записи.**
Диагностика STORE/RETRIEVE различает: store запрошен → завершение на
воркере/адаптере → объект принят в L2 → запись L2 успешна/не удалась →
объектов/байтов сохранено; retrieve запрошен → завершён / прерван /
предзагружен из L2 / невалидный объект отклонён. Фактические доказательства
персистентности — строки «L2 store completed» / «L2 prefetch load completed» в
`lmcache-server.log`.

## 4. Безопасный полный сброс кэша (разовая миграция)

`scripts/reset-persistent-cache.sh` — ЕДИНСТВЕННАЯ деструктивная операция, её
никогда не вызывают start/stop/restart:

```
KVP_RESET_CONFIRM=FULL-RESET bash scripts/reset-persistent-cache.sh
```

Требования: явная строка подтверждения; сервисы заранее остановлены (или
`KVP_RESET_FORCE_STOP=1`); каждый путь разрешается внутри настроенного корня
кэша; `/`, домашние каталоги, каталоги моделей, репозитории, артефакты сборки
и симлинки за пределы корня отклоняются. Отчёт о сбросе (очищенные пути с
числом байт, подтверждение сохранности посторонних данных) пишется в
`live/cache-reset-report-<UTC>.txt`.

### Маркер корня кэша (`.kvp-cache-root-marker`)

`start-cache.sh` ставит на каталог корня кэша метку `.kvp-cache-root-marker`
при инициализации кэша. Маркер — это способ, которым деструктивный reset
доказывает, что цель действительно является каталогом кэша: явно заданный
`KVP_CACHE_ROOT` принимается либо при наличии маркера, либо когда все записи
верхнего уровня каталога — только известные артефакты LMCache/kvpersist
(`*.data`, `*.tmp`, `*.bin`, `*.bin.tmp.*`, `objects/`, `kvp-*`, `index.db`,
`crash-*`, `lmcache-server.log/.pid`, `l2-adapter.rendered.json`,
`engine-identity.env`, `.kvp-active-writes`, а также имя `relative_tmp_dir`,
заданное в `l2-adapter.rendered.json`). Любая посторонняя запись верхнего
уровня приводит к отказу, и отказ перечисляет эти записи.

**Для кэша, созданного до появления маркера** (старые развёртывания):
сначала посмотрите содержимое каталога, затем проставьте маркер вручную:

```
ls -A <KVP_CACHE_ROOT>
touch <KVP_CACHE_ROOT>/.kvp-cache-root-marker
```

Делайте это только тогда, когда `ls -A` показывает исключительно данные
кэша. Чтобы очистить каталог кэша с нестандартным содержимым, сначала
уберите посторонние файлы (или ставьте маркер только при полной уверенности,
что это данные кэша).

Независимо от маркера reset **всегда** отказывает: `$HOME` и любому
домашнему каталогу пользователя (`/root`, `/home/<user>`), любому предку
каталога репозитория и любому каталогу с `.git` на глубине вложенности ≤ 2.

НИКОГДА не удалять: веса модели, конфигурацию модели, данные приложения,
рецепты, исходный код, артефакты сборки LMCache, диагностические логи,
посторонние файлы на NVMe и саму точку монтирования NVMe.

## 5. Диагностика промахов кэша / сбоев

* Проверка персистентного round-trip: `scripts/test-recovery.sh` — холодный
  baseline пишет в L2, рестарт только движка восстанавливает из L1, полный
  рестарт (движок + кэш-сервер) обязан показать события чтения L2 и ненулевые
  `cached_tokens`. Успешная генерация — не доказательство кэш-попадания;
  проверяйте cached tokens и события L2.
* Отклонение неполных объектов: пропуски STORE с «invalid object» в логе
  означают, что #5972 отклонил объект, чья сохранённая геометрия содержала
  нулевой блок; такие объекты никогда не появятся кэш-попаданиями.
* Сбой бэкенда: ошибки записи L2 сообщаются как ошибки — движок никогда молча
  не рапортует их как сохранённые.
* `KVP_LOAD_FAILURE_POLICY` остаётся ЭКСПЕРИМЕНТАЛЬНЫМ (дефолт `fail`).
  Старое предупреждение о паре с защитой C2 устарело: гонка retrieve/abort
  обрабатывается внутри кастомной сборки (downstream-порт PR #4136), локальной
  защиты для конфликта больше нет. **Статус (честно): implemented, unit-tested;
  hardware validation: C0-C10 passed on the operator's host (new base
  07f1c6bb6), C11 (4136 test fixtures) fixed in r16, C12-C16 pending.**
  Фикс гонки, кастомная сборка и нативное расширение cuda_ops проверены
  юнит-тестами в этом репозитории; эталонный пайплайн сборки прошёл C0-C10
  на стенде оператора на новой базе (wheel собран, cuda_ops содержит sm_120,
  четыре нативных расширения, установка прошла, import-check OK из
  установленного дистрибутива, staging OK) и остановился на C11 на устаревших
  фикстурах тестов 4136 (исправлено в r16: атрибуты фикстур + заглушки
  num_computed_tokens, устаревший _pending_store переписан под API
  lazy-offload менеджера) — C12-C16 ещё предстоит выполнить: см. оставшийся
  план стендовой валидации в разделе 5b «Этапы внедрения» (тесты →
  identity/provenance → build-image → запуск движка и кэш-сервера →
  параллельная нагрузка с аборт-ами).

## 6. Будущая миграция в апстрим (когда #4136 и #5972 сольют)

Плановое продолжение, не часть текущего развёртывания. Когда оба PR будут
слиты в апстрим:

1. Изучить апстрим-коммиты слияния; выбрать новый закреплённый коммит или
   официальный релиз LMCache, содержащий #6551, #4136 и #5972 (проверить по
   истории Git и исходникам — новый релиз не содержит их автоматически).
2. Удалить `vendor/patches/lmcache/4136-mp-retrieve-abort.patch`.
3. Удалить `vendor/patches/lmcache/5972-incomplete-kv-objects.patch`.
4. Удалить их записи о применении из `scripts/build-lmcache.sh` и строки о
   ревизиях из `SOURCES.md` / `provenance.json`.
5. Удалить дублируемые апстримом тесты временных патчей; проектные
   интеграционные тесты сохранить.
6. Обновить этот документ и три README: #4136/#5972 больше не «downstream
   patches».
7. Собрать новый LMCache против того же закреплённого vLLM; проверить, что
   MP-сервер и каждый воркер сообщают ОДНУ и ту же новую идентичность сборки.
8. Прогнать полный регрессионный набор и повторить аппаратную валидацию
   (персистентные STORE / RETRIEVE после полного рестарта, стресс отмен,
   поведение LRU под давлением L1), прежде чем выводить текущую сборку.

Не удалять текущую кастомную сборку и её патчи, пока замена не пройдёт эту
валидацию. После миграции в репозиториях проекта не должно остаться вручную
скопированных реализаций #4136/#5972.