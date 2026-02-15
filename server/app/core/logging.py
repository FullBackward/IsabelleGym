import logging
import os
from logging.handlers import RotatingFileHandler

from server.app.core.config import Logging

BASE_LOGGER_NAME = "isabelleserver"

def setup_logging() -> None:
    base = logging.getLogger(BASE_LOGGER_NAME)

    # Prevent configuring twice (common with reloaders/imports)
    if getattr(base, "_configured", False):
        return

    base.setLevel(getattr(logging, Logging.LOG_LEVEL))
    base.propagate = False  # base should not bubble up to root

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    base.addHandler(console)

    # File handler (rotating)
    os.makedirs(Logging.LOG_DIR, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(Logging.LOG_DIR, Logging.LOG_FILE),
        maxBytes=Logging.MAX_LOG_SIZE_BYTES,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)
    base.addHandler(file_handler)

    base._configured = True


def get_logger(module_name: str) -> logging.Logger:
    setup_logging()
    logger = logging.getLogger(f"{BASE_LOGGER_NAME}.{module_name}")

    # Let it inherit level from base unless you explicitly override it
    logger.setLevel(logging.NOTSET)
    logger.propagate = True  # IMPORTANT: propagate to base logger handlers

    return logger
