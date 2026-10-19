import logging
import re
from logging.handlers import RotatingFileHandler


class SecretFilter(logging.Filter):
    def __init__(self, settings):
        super().__init__()
        self.values = [
            settings.bot_token,
            settings.api_hash,
            settings.session_encryption_key,
            settings.database_url,
        ]

    def filter(self, record):
        text = record.getMessage()
        for value in self.values:
            if value:
                text = text.replace(value, "[secret]")
        text = re.sub(r"(https?|socks5)://[^\s/@]+:[^\s@]+@", r"\1://[credentials]@", text)
        text = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", "[bot-token]", text)
        text = re.sub(r"\+\d{8,15}\b", "[phone]", text)
        text = re.sub(r"\b1[A-Za-z0-9_=-]{340,}\b", "[session]", text)
        record.msg, record.args = text, ()
        # Never serialize arbitrary exception payloads from network libraries.
        if record.exc_info:
            record.msg += f" [{record.exc_info[0].__name__}]"
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging(settings):
    settings.ensure_directories()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    handlers = [
        RotatingFileHandler(
            settings.logs_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(SecretFilter(settings))
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    for name in ("telethon", "httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
