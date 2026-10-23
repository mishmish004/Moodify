import logging
import sys
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
# No settings import: modules create loggers at import time, and that must not
# require a valid .env.

class MoodifyLogger:
    # 1. Anchor Logs to Project Root (Not where script is run)
    # This finds the file path of THIS file, goes up 3 levels (core -> src -> root)
    BASE_DIR = Path(__file__).resolve().parent.parent.parent
    LOG_DIR = BASE_DIR / "logs"
    LOG_FILE = LOG_DIR / "moodify.log"
    
    # 2. Production Constants
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB
    BACKUP_COUNT = 3

    @classmethod
    def setup(cls):
        """Ensures the log directory exists before any logger is created."""
        os.makedirs(cls.LOG_DIR, exist_ok=True)

    @staticmethod
    def get_logger(service_name: str) -> logging.Logger:
        # Ensure dir exists first
        MoodifyLogger.setup()

        # Naming Convention: moodify.searcher, moodify.api, etc.
        logger_name = f"moodify.{service_name}"
        logger = logging.getLogger(logger_name)

        # 3. Idempotency Check (Prevent Duplicate Logs)
        # If this logger already has its own handlers, just return it. Not hasHandlers(): that also
        # counts the root logger's, so any root setup (pytest, basicConfig) left INFO unconfigured.
        if logger.handlers:
            return logger

        # 4. Debug goes to the file, the console stays at INFO (handlers below)
        log_level = logging.DEBUG
        logger.setLevel(log_level)

        # 5. Formatter (Standardized)
        formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # 6. File Handler (Rotating)
        file_handler = RotatingFileHandler(
            MoodifyLogger.LOG_FILE,
            maxBytes=MoodifyLogger.MAX_BYTES,
            backupCount=MoodifyLogger.BACKUP_COUNT,
            encoding='utf-8' # Always define encoding for Windows compatibility
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)

        # 7. Console Handler (For Docker Logs)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO) # Keep console clean, keep debug in file

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
        # Prevent propagation to root logger (avoid double printing)
        logger.propagate = False 

        return logger