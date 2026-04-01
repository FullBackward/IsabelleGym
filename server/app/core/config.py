import os
from typing import Final
import re


class API:
    VERSION: Final = "0.0.1"


class Server:
    DEFAULT_POOL_SIZE: Final = 16
    ENABLE_MEMORY_MANAGEMENT: Final = True
    ENABLE_CACHE: Final = False
    INITIAL_SESSIONS: Final = 8
    MAX_CACHE_SIZE: Final = 1
    SHOW_STATES: Final = False
    DEFAULT_FIELD: Final = "HOL"
    IDLE_TIMEOUT_SECONDS: Final = 1800


class RegularExp:
    IMPORT_RE = re.compile(r'(?ms)\bimports\b(?P<imports>.*?)\bbegin\b')
    IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')
    THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
    


class Logging:
    LOG_LEVEL: Final = os.getenv("ISABELLE_SERVER_LOG_LEVEL", "INFO").upper()
    LOG_DIR: Final = os.getenv("ISABELLE_SERVER_LOG_DIR", "logs")
    LOG_FILE: Final = os.getenv("ISABELLE_SERVER_LOG_FILE", "server.log")
    MAX_LOG_SIZE_BYTES: Final = int(
        os.getenv("ISABELLE_SERVER_MAX_LOG_SIZE_BYTES", str(10 * 1024 * 1024))
    )
    BACKUP_COUNT: Final = int(os.getenv("ISABELLE_SERVER_LOG_BACKUP_COUNT", "5"))
    ENABLE_FILE_LOGGING: Final = os.getenv("ISABELLE_SERVER_ENABLE_FILE_LOGGING", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    REQUEST_HEADER_NAME: Final = os.getenv("ISABELLE_SERVER_REQUEST_ID_HEADER", "X-Request-ID")
    COMMAND_PREVIEW_CHARS: Final = int(os.getenv("ISABELLE_SERVER_COMMAND_PREVIEW_CHARS", "120"))
    THEORY_PREVIEW_CHARS: Final = int(os.getenv("ISABELLE_SERVER_THEORY_PREVIEW_CHARS", "160"))
