import logging
from dataclasses import dataclass


def init_logging(level: str):
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        level=getattr(logging, level, logging.WARNING),
    )


@dataclass
class NavServerInfo:
    """Information about a NAV server instance"""

    url: str
    owner_id: int
    tenant_id: int


def url_with_https(url: str):
    if not url.lower().startswith("https://"):
        return "https://" + url
    return url


def url_with_http(url: str):
    if not url.lower().startswith("http://"):
        return "http://" + url
    return url
