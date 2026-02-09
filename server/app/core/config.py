from typing import Final

class API:
    VERSION: Final = "0.0.1"


class Server:
    DEFAULT_POOL_SIZE: Final = 8
    ENABLE_MEMORY_MANAGEMENT: Final = True
    ENABLE_CACHE: Final = True

class Logging:
    LOG_DIR: Final = 'server/app/logs'
    LOG_LEVEL: Final = 'DEBUG'
    LOG_FILE: Final = 'server.log'
    MAX_LOG_SIZE_BYTES: Final = 10 * 1024 * 1024