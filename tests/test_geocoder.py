from unittest.mock import MagicMock, patch

from navsync.geocoder import geocode_address


def make_api_response(total_hits: int, lat: float = 0.0, lon: float = 0.0) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "metadata": {"totaltAntallTreff": total_hits},
        "adresser": [{"representasjonspunkt": {"lat": lat, "lon": lon}}]
        if total_hits > 0
        else [],
    }
    return response


def patch_api(total_hits: int, lat: float = 0.0, lon: float = 0.0):
    return patch(
        "navsync.geocoder.requests.get",
        return_value=make_api_response(total_hits, lat, lon),
    )


class TestGeocodeAddress:
    def test_single_result_returns_lat_lon(self):
        with patch_api(1, lat=60.123456, lon=10.456789):
            assert geocode_address("Testgata 1, 0001 Oslo") == (60.123456, 10.456789)

    def test_no_results_returns_none(self):
        with patch_api(0):
            assert geocode_address("Testgata 1, 0001 Oslo") is None

    def test_multiple_results_returns_none(self):
        with patch_api(2):
            assert geocode_address("Testgata 1") is None
