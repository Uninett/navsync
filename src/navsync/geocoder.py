import functools
import json
import logging
import os
import tempfile
import time

import requests

from navsync.config import settings

_logger = logging.getLogger(__name__)

_KARTVERKET_URL = "https://ws.geonorge.no/adresser/v1/sok"


def _cache_path() -> str:
    return os.path.expanduser(settings.geocode.cache_path)


def _load_cache() -> dict:
    try:
        with open(_cache_path()) as f:
            cache = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        _logger.warning("Ignoring unreadable geocode cache: %s", e)
        return {}
    if not isinstance(cache, dict):
        return {}
    # Drop malformed entries so a damaged file can't break lookups
    return {
        address: entry
        for address, entry in cache.items()
        if isinstance(entry, dict) and "coords" in entry and "fetched_at" in entry
    }


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write to a temporary file first so an interrupted run can't leave a
        # half-written cache behind
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError as e:
        _logger.warning("Could not write geocode cache: %s", e)


def _disk_cached(func):
    """
    Cache the results of a geocoding function on disk, keyed by address.
    Does nothing unless geocode.cache_enabled is set.
    """

    @functools.wraps(func)
    def wrapper(address: str):
        if not settings.geocode.cache_enabled:
            return func(address)

        cache = _load_cache()
        entry = cache.get(address)
        if entry is not None:
            coords = entry["coords"]
            if time.time() - entry["fetched_at"] <= settings.geocode.cache_ttl:
                return tuple(coords) if coords else None

        coords = func(address)
        cache[address] = {"coords": coords, "fetched_at": time.time()}
        _save_cache(cache)
        return coords

    return wrapper


@_disk_cached
def geocode_address(address: str) -> tuple[float, float] | None:
    """
    Look up coordinates for a Norwegian address. Returns (latitude, longitude) if one address is found.
    If no address or multiple addresses are found, returns None.

    If geocode.cache_enabled is set, results are cached on disk, so repeated runs
    do not query Kartverket again for the same address. Failed requests are not
    cached.
    """
    params = {
        "sok": address,
        "utkoordsys": "4258",
        "treffPerSide": "2",
        "asciiKompatibel": "true",
    }
    headers = {"User-Agent": settings.geocode.user_agent}
    response = requests.get(_KARTVERKET_URL, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    total = data.get("metadata", {}).get("totaltAntallTreff", 0)
    if total == 0:
        _logger.warning("No geocoding results for address: %r", address)
        return None
    if total > 1:
        _logger.warning(
            "Ambiguous address (%d results), skipping geocoding: %r", total, address
        )
        return None

    point = data["adresser"][0]["representasjonspunkt"]
    return point["lat"], point["lon"]
