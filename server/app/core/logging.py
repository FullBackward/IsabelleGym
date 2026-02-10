import logging
import os
from server.app.core.config import Logging
import threading
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("isabellegym")
logger.setLevel(getattr(logging, Logging.LOG_LEVEL))

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

os.makedirs(Logging.LOG_DIR, exist_ok=True)

file_handler = RotatingFileHandler(
    os.path.join(Logging.LOG_DIR, Logging.LOG_FILE),
    maxBytes = Logging.MAX_LOG_SIZE_BYTES,
    backupCount = 5
)

file_handler.setFormatter(formatter)

file_handler.createLock()

logger.addHandler(file_handler)
