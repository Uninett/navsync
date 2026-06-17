import logging

import requests

_logger = logging.getLogger(__name__)

_KARTVERKET_URL = "https://ws.geonorge.no/adresser/v1/sok"


def geocode_address(address: str) -> tuple[float, float] | None:
    """
    Look up coordinates for a Norwegian address. Returns (latitude, longitude) if one address is found.
    If no address or multiple addresses are found, returns None.
    """
    params = {
        "sok": address,
        "utkoordsys": "4258",
        "treffPerSide": "2",
        "asciiKompatibel": "true",
    }
    response = requests.get(_KARTVERKET_URL, params=params, timeout=10)
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
