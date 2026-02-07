import logging
import os
from server.app.core.config import Logging

logger = logging.getLogger("isabellegym")
logger.setLevel(getattr(logging, Logging.LOG_LEVEL))

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

os.makedirs(Logging.LOG_DIR, exist_ok=True)
file_handler = logging.FileHandler(os.path.join(Logging.LOG_DIR, Logging.LOG_FILE))
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)