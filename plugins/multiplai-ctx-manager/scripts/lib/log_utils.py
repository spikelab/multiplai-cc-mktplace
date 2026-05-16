"""Logging utilities for multiplai plugin.

All log files are written under the plugin data directory via path resolver.
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def _get_logs_dir() -> Path:
    """Get logs directory from path resolver."""
    # Import here to avoid circular imports at module level
    from lib.paths import get_paths
    return get_paths().logs_dir()


def setup_logging(name: str = "multiplai", level: int = logging.INFO) -> logging.Logger:
    """Set up logging for a multiplai script.

    Creates the log directory if it doesn't exist and configures
    both file and stderr handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    # Stderr handler for immediate feedback
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(stderr_handler)

    # File handler in plugin data dir
    try:
        logs_dir = _get_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = logs_dir / f"{name}-{today}.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(file_handler)
    except Exception:
        logger.debug("Could not set up file logging", exc_info=True)

    return logger
