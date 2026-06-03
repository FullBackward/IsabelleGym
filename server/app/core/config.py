import os
from typing import Final
import re


class API:
    VERSION: Final = "0.0.2"


class Server:
    DEFAULT_POOL_SIZE: Final = int(os.getenv("ISABELLE_POOL_SIZE", "24"))
    ENABLE_MEMORY_MANAGEMENT: Final = os.getenv("ISABELLE_ENABLE_MEMORY_MANAGEMENT", "true").lower() in {
        "1", "true", "yes", "on",
    }
    ENABLE_CACHE: Final = os.getenv("ISABELLE_ENABLE_CACHE", "false").lower() in {
        "1", "true", "yes", "on",
    }
    INITIAL_SESSIONS: Final = int(os.getenv("ISABELLE_INITIAL_SESSIONS", "3"))
    MAX_CACHE_SIZE: Final = int(os.getenv("ISABELLE_MAX_CACHE_SIZE", "1"))
    SHOW_STATES: Final = os.getenv("ISABELLE_SHOW_STATES", "false").lower() in {
        "1", "true", "yes", "on",
    }
    DEFAULT_FIELD: Final = os.getenv("ISABELLE_DEFAULT_FIELD", "HOL")
    HOST: Final = os.getenv("ISABELLE_SERVER_HOST", "0.0.0.0")
    PORT: Final = int(os.getenv("ISABELLE_SERVER_PORT", "8000"))
    MAX_LEASE_AGE: Final = int(os.getenv("ISABELLE_MAX_LEASE_AGE", "7200")) # 2 hours

class Repl:
    SUBGOALS_TIMEOUT_S: Final       = int(os.getenv("ISABELLE_REPL_SUBGOALS_TIMEOUT", "20"))
    LOCAL_FACTS_TIMEOUT_S: Final    = int(os.getenv("ISABELLE_REPL_LOCAL_FACTS_TIMEOUT", "20"))
    GLOBAL_FACTS_TIMEOUT_MIN: Final = int(os.getenv("ISABELLE_REPL_GLOBAL_FACTS_TIMEOUT_MINUTES", "5"))

    GATEWAY_POLL_INTERVAL: Final    = float(os.getenv("ISABELLE_REPL_GATEWAY_POLL_INTERVAL", "0.1"))
    GATEWAY_POLL_TIMEOUT: Final     = float(os.getenv("ISABELLE_REPL_GATEWAY_POLL_TIMEOUT", "20.0"))
    GATEWAY_TERMINATE_WAIT: Final   = float(os.getenv("ISABELLE_REPL_GATEWAY_TERMINATE_WAIT", "3.0"))

    BACKEND_EXIT_TIMEOUT: Final     = float(os.getenv("ISABELLE_BACKEND_EXIT_TIMEOUT", "60.0"))
    BACKEND_JOIN_TIMEOUT: Final     = float(os.getenv("ISABELLE_BACKEND_JOIN_TIMEOUT", "5.0"))
    BACKEND_QUEUE_POLL: Final       = float(os.getenv("ISABELLE_BACKEND_QUEUE_POLL", "0.1"))


class Memory:
    PRESSURE_THRESHOLD: Final   = float(os.getenv("ISABELLE_MEMORY_PRESSURE_THRESHOLD", "85.0"))
    MIN_AVAILABLE_MB: Final     = int(os.getenv("ISABELLE_MEMORY_MIN_AVAILABLE_MB", "256"))
    SESSION_COUNT_LIMIT: Final  = int(os.getenv("ISABELLE_MEMORY_SESSION_COUNT_LIMIT", "20"))
    GC_TRIGGER_THRESHOLD: Final = float(os.getenv("ISABELLE_MEMORY_GC_TRIGGER_THRESHOLD", "90.0"))
    GC_SLEEP_MS: Final          = int(os.getenv("ISABELLE_MEMORY_GC_SLEEP_MS", "100"))
    FALLBACK_SYSTEM_MB: Final   = int(os.getenv("ISABELLE_MEMORY_FALLBACK_SYSTEM_MB", "4096"))
    SERVER_START_RETRIES: Final = int(os.getenv("ISABELLE_SERVER_START_RETRIES", "5"))


class Timeouts:
    COMMAND_DEFAULT: Final      = float(os.getenv("ISABELLE_TIMEOUT_COMMAND", "30.0"))
    BIGSTEP_DEFAULT: Final      = float(os.getenv("ISABELLE_TIMEOUT_BIGSTEP", "300.0"))
    IDLE_DEFAULT: Final       = float(os.getenv("ISABELLE_TIMEOUT_STATUS", "300.0"))
    PROOF_STATE: Final          = float(os.getenv("ISABELLE_TIMEOUT_PROOF_STATE", "30.0"))
    CHECKPOINT_SAVE: Final      = float(os.getenv("ISABELLE_TIMEOUT_CHECKPOINT_SAVE", "30.0"))
    CHECKPOINT_RESTORE: Final   = float(os.getenv("ISABELLE_TIMEOUT_CHECKPOINT_RESTORE", "30.0"))
    CLEANUP_INTERVAL: Final     = int(os.getenv("ISABELLE_CLEANUP_INTERVAL", "60"))
    SESSION_IDLE_TIMEOUT: Final = int(os.getenv("ISABELLE_IDLE_TIMEOUT", "1800"))

class RegularExp:
    IMPORT_RE = re.compile(r'(?ms)\bimports\b(?P<imports>.*?)\bbegin\b')
    IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')
    THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
    THEORY_HEADER_RE = re.compile(r'(?ms)\btheory\b.+?\bimports\b.+?\bbegin\b')


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
