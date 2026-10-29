import re
from urllib.parse import urlparse

from app.core.config import settings

_CREDENTIALS_IN_URL = re.compile(r"://([^:/@\s]+):[^@\s]+@")


def mask_secrets(text: str) -> str:
    """Mask any URL password so a connection secret never appears in output or logs."""
    for url in (settings.database_url, settings.redis_url):
        try:
            password = urlparse(url).password
        except ValueError:
            password = None
        if password:
            # Only the userinfo form `:password@`, never a substring of the scheme.
            text = text.replace(f":{password}@", ":***@")
    return _CREDENTIALS_IN_URL.sub(r"://\1:***@", text)
