import logging


def init_logging(level: str):
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        level=getattr(logging, level, logging.WARNING),
    )
