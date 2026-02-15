from typing import Final

class API:
    VERSION: Final = "0.0.1"


class Server:
    DEFAULT_POOL_SIZE: Final = 8
    ENABLE_MEMORY_MANAGEMENT: Final = True
    ENABLE_CACHE: Final = False
    INITIAL_SESSIONS: Final = 4
    MAX_CACHE_SIZE: Final = 1
    SHOW_STATES: Final = False