import logging

logger = logging.getLogger(__name__)

LOG_LEVELS = {
    "info": (logging.INFO, "[.]"),
    "success": (logging.INFO, "[+]"),
    "warning": (logging.WARNING, "[!]"),
    "error": (logging.ERROR, "[x]"),
    "debug": (logging.DEBUG, "[•]"),
}


def log(msg: str, level: str = "info") -> None:
    lvl, prefix = LOG_LEVELS.get(level, (logging.INFO, "[.]"))
    logger.log(lvl, f"{prefix} {msg}")
