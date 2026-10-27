> Локализованная версия configs/generic/lmcache-server.env — комментарии переведены.
> Имена переменных, значения и порядок совпадают с английской эталонной версией.

# lmcache-server.env — настройки отдельного процесса lmcache-сервера.
# Используются scripts/start-cache.sh. Конфиг L2-адаптера собирается из значений
# KVP_*, чтобы был один источник истины (configs/generic/cache.env).

# Порт запросов MP-сервера (сюда подключается vLLM; ZMQ).
LMCACHE_HOST=127.0.0.1
LMCACHE_PORT=5555

# HTTP-фронтенд для здоровья / метрик / администрирования. Только loopback:
# не подвергать неаутентифицированный управление доступным интерфейсам.
LMCACHE_HTTP_HOST=127.0.0.1
LMCACHE_HTTP_PORT=8080

# Уровень L1 (RAM хоста); постоянный уровень — L2 на общем диске ВМ.
LMCACHE_L1_SIZE_GB=64

# Гранулярность чанка в токенах (256 — задокументированный продовый дефолт).
LMCACHE_CHUNK_SIZE=256

# Политика эвикции для ОБОИХ уровней. trigger_watermark — мягкий порог как доля
# бюджета уровня; eviction_ratio — сколько возвращается при срабатывании.
# Жёсткую границу L2 (max_capacity_gb) независимо обеспечивает адаптер
# nixl_store_dynamic.
LMCACHE_EVICTION_POLICY=LRU
LMCACHE_EVICTION_TRIGGER_WATERMARK=0.9
LMCACHE_EVICTION_RATIO=0.1

# Serde для дискового уровня L2: fp8 вдвое уменьшает байты на токен ценой
# микро-сдвига качества декодирования. «none» — байт-точный KV.
LMCACHE_L2_SERDE=none