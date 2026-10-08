import logging
from pathlib import Path

from app.services.config import settings

def init_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    error_log_path = Path(settings.ERROR_LOG_PATH).expanduser().resolve()
    error_log_path.parent.mkdir(parents=True, exist_ok=True)
    proxyapi_logger = logging.getLogger("proxyapi")
    proxyapi_logger.setLevel(logging.ERROR)
    proxyapi_logger.propagate = False

    if not any(isinstance(handler, logging.FileHandler) and Path(getattr(handler, "baseFilename", "")) == error_log_path for handler in proxyapi_logger.handlers):
        file_handler = logging.FileHandler(error_log_path, encoding="utf-8")
        file_handler.setLevel(logging.ERROR)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        proxyapi_logger.addHandler(file_handler)

logger = logging.getLogger("proxyapi_bots")
