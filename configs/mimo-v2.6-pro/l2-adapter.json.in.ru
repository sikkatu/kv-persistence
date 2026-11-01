// Локализованная версия configs/mimo-v2.6-pro/l2-adapter.json.in — комментарии переведены.
// Плейсхолдеры и ключи JSON совпадают с английской эталонной версией.

// Шаблон спецификации L2-адаптера для kv-persistence на MiMo-V2.6-Pro.
// nixl_store_dynamic — ЕДИНСТВЕННЫЙ семейству адаптеров LMCache с документированной
// семантикой persist/recover: на старте индекс в памяти пуст, а каждый поиск при
// промахе уходит к детерминированному файлу на диске (ленивая пересборка индекса).
// persist_enabled указан явно, чтобы значение не менялось молча при обновлениях.
//
// backend: POSIX — обычный файловый ввод-вывод, работает на любой локальной ФС.
//   GDS здесь СОЗНАТЕЛЬНО не используется: сначала простейший надёжный путь к NVMe,
//   пока персистентность не доказана; топология PCIe хоста (Gen 4, одна карта x8)
//   от GDS не выиграла бы.
//
// file_path — ДОЛЖЕН быть на локальном NVMe (KVP_CACHE_ROOT). Никогда на сетевом
//   монтировании.
// max_capacity_gb — ЖЁСТКИЙ бюджет; записи сверх него отклоняются. Обязателен,
//   чтобы контроллер эвикции считал занятое. По умолчанию 750, потолок 1 ТБ.
//
// eviction — опционален для каждого адаптера. LRU по ключам KV-чанков; фоновый
//   поток следит за get_usage() и, выше trigger_watermark, вычищает на
//   eviction_ratio. Это автоматический путь обслуживания; более точная value-based
//   политика — implementation/kvpersist/retention.py (ранжирует записи, когда
//   доступна статистика по ним).
//
// serde — LOSSLESS-кодек хранения, подставляется из __KVP_L2_SERDE_JSON__.
//   Дефолт — НИЧЕГО (нет ключа "serde"): чанки пишутся байт-в-байт, идентично
//   несжатому upstream-пути. Кодек выбирается через
//   LMCACHE_L2_SERDE=zstd|lz4 (канонический ключ, его читают все лаунчеры) или
//   короткую запись USE_LOSSLESS_SERDE=1; KVP_L2_SERDE принимается как алиас.
//   Фрагмент рендерит блок "serde" с lossless-кодеками, которые регистрирует
//   kvpersist.compress_serde (kvp-zstd / kvp-lz4).
//   Lossy-квантизации в этом шаблоне НЕТ: fp8 и turboquant сознательно
//   отсутствуют и должны отсутствовать (требование задачи:
//   «никакого fp8 / turboquant / lbq по умолчанию»).
//   Отрендеренный фрагмент — "" для lossless-raw режима (нет ключа serde) и
//   `"serde": { ... },` при выбранном кодеке.
{
  "type": "nixl_store_dynamic",
  "backend": "POSIX",
  "backend_params": {
    "file_path": "__KVP_CACHE_ROOT__",
    "use_direct_io": "false",
    "max_capacity_gb": "__KVP_CACHE_BUDGET_GB__"
  },
  "persist_enabled": true,
  __KVP_L2_SERDE_JSON__
  "eviction": {
    "eviction_policy": "LRU",
    "trigger_watermark": __KVP_EVICT_TRIGGER_WATERMARK__,
    "eviction_ratio": __KVP_EVICT_RATIO__
  }
}