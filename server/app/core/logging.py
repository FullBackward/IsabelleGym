import logging
import os
from isort import file 

logger = logging.getLogger("isabellegym")
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

log_dir = 'logs'
os.makedirs(log_dir, exist_ok=True)  # Create logs/ if it doesn't exist
file_handler = logging.FileHandler(os.path.join(log_dir, 'server.log'))
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)