import logging
import os
import sys
from logging.handlers import RotatingFileHandler

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import LOG_LEVEL, LOG_DIR, LOG_FILE_MAX_SIZE, LOG_FILE_BACKUP_COUNT


_logger_instance = None


def _setup_logger() -> logging.Logger:
    global _logger_instance
    
    if _logger_instance is not None:
        return _logger_instance
    
    logger = logging.getLogger("onto_eval")
    logger.setLevel(getattr(logging, LOG_LEVEL))
    
    logger.handlers.clear()
    
    os.makedirs(LOG_DIR, exist_ok=True)
    
    file_handler = RotatingFileHandler(
        filename=f"{LOG_DIR}/app.log",
        maxBytes=LOG_FILE_MAX_SIZE,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setLevel(getattr(logging, LOG_LEVEL))
    
    formatter = logging.Formatter(
        '%(asctime)s,%(msecs)03d [%(levelname)s] [%(node)s] %(message)s | %(context)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    _logger_instance = logger
    return logger


def _get_logger() -> logging.Logger:
    return _setup_logger()


def log(level: str, node: str, message: str, **kwargs) -> None:
    logger = _get_logger()
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    context = ", ".join([f"{k}={v}" for k, v in kwargs.items()])
    
    extra = {"node": node, "context": context}
    logger.log(log_level, message, extra=extra)
    
    for handler in logger.handlers:
        handler.flush()
