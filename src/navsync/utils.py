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
