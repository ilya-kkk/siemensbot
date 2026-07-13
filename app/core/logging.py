import logging
import time

from app.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure consistent UTC stdout logging for every process."""
    logging.Formatter.converter = time.gmtime
    level = getattr(logging, settings.log_level.strip().upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
