import os
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from platformdirs import user_cache_dir

from navsync import geocoder
from navsync.geocoder import geocode_address


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(
        geocoder.settings,
        "GEOCODE",
        {
            "cache_enabled": True,
            "cache_path": str(tmp_path / "geocode.json"),
            "cache_ttl": 60,
        },
    )
    return tmp_path


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


class TestGeocodeCache:
    def test_second_lookup_uses_cache(self):
        with patch_api(1, lat=60.0, lon=10.0) as get:
            assert geocode_address("Testgata 1") == (60.0, 10.0)
            assert geocode_address("Testgata 1") == (60.0, 10.0)
        assert get.call_count == 1

    def test_misses_are_cached(self):
        with patch_api(0) as get:
            assert geocode_address("Nowhere 1") is None
            assert geocode_address("Nowhere 1") is None
        assert get.call_count == 1

    def test_expired_entry_is_refetched(self):
        with patch_api(1, lat=60.0, lon=10.0):
            geocode_address("Testgata 1")
        later = time.time() + 61
        with patch("navsync.geocoder.time.time", return_value=later):
            with patch_api(1, lat=61.0, lon=11.0) as get:
                assert geocode_address("Testgata 1") == (61.0, 11.0)
        assert get.call_count == 1

    def test_corrupt_cache_is_ignored(self, cache_dir):
        (cache_dir / "geocode.json").write_text("not json")
        with patch_api(1, lat=60.0, lon=10.0):
            assert geocode_address("Testgata 1") == (60.0, 10.0)

    def test_malformed_entries_are_ignored(self, cache_dir):
        (cache_dir / "geocode.json").write_text('{"Testgata 1": {"coords": [1, 2]}}')
        with patch_api(1, lat=60.0, lon=10.0):
            assert geocode_address("Testgata 1") == (60.0, 10.0)

    def test_nothing_is_cached_when_disabled(self, cache_dir):
        with patch.dict(geocoder.settings, {"GEOCODE": {"cache_enabled": False}}):
            with patch_api(1, lat=60.0, lon=10.0) as get:
                geocode_address("Testgata 1")
                geocode_address("Testgata 1")
        assert get.call_count == 2
        assert list(cache_dir.iterdir()) == []

    def test_request_errors_are_not_cached(self):
        with patch(
            "navsync.geocoder.requests.get",
            side_effect=requests.ConnectionError("down"),
        ):
            with pytest.raises(requests.ConnectionError):
                geocode_address("Testgata 1")
        with patch_api(1, lat=60.0, lon=10.0) as get:
            assert geocode_address("Testgata 1") == (60.0, 10.0)
        assert get.call_count == 1


class TestCacheSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.undo()
        assert geocoder.settings.geocode.cache_path == os.path.join(
            user_cache_dir("navsync"), "geocode.json"
        )
        assert geocoder.settings.geocode.cache_enabled is False
        assert geocoder.settings.geocode.cache_ttl == 30 * 24 * 3600

    def test_configured_cache_path_is_used(self, monkeypatch, tmp_path):
        monkeypatch.undo()
        target = tmp_path / "custom.json"
        with patch.dict(geocoder.settings, {"GEOCODE": {"cache_path": str(target)}}):
            assert geocoder._cache_path() == str(target)
