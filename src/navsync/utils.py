import logging
import re
from dataclasses import dataclass


def init_logging(level: str):
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        level=getattr(logging, level, logging.WARNING),
    )


@dataclass
class NavServerInfo:
    """Information about a NAV server instance"""

    id: int
    url: str
    owner_id: int
    tenant_id: int


def url_with_https(url: str):
    if not url.lower().startswith("https://"):
        url = "https://" + url
    if not url.endswith("/"):
        url += "/"
    return url


def url_with_http(url: str):
    if not url.lower().startswith("http://"):
        url = "http://" + url
    if not url.endswith("/"):
        url += "/"
    return url


def sanitize_slug(slug: str) -> str:
    """Sanitizes a string to be used as a slug by converting to lowercase, replacing spaces and `.` with hyphens and removing any characters that are not alphanumeric, underscores, or hyphens."""
    slug = slug.replace(" ", "-").lower()
    slug = slug.replace(".", "-")
    return re.sub("[^0-9a-zA-Z_-]+", "", slug)
