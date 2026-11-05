from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from navsync.parser import NAV_TOKEN_CLOCK_SKEW, NavFetcher


@pytest.fixture(scope="module")
def private_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def claims(private_key):
    fetcher = NavFetcher(
        private_key=private_key,
        expiry_delta=timedelta(hours=1),
        issuer="navsync",
        netbox_tenants={},
        categories=[],
    )
    token = fetcher.generate_nav_token(aud="https://nav.example.org")
    return jwt.decode(token, options={"verify_signature": False})


class TestGenerateNavToken:
    def test_token_should_be_valid_before_now_to_allow_for_clock_skew(
        self, private_key
    ):
        now = datetime.now(timezone.utc).timestamp()
        token_claims = claims(private_key)
        expected = now - NAV_TOKEN_CLOCK_SKEW.total_seconds()
        assert token_claims["nbf"] == pytest.approx(expected, abs=5)
        assert token_claims["iat"] == pytest.approx(expected, abs=5)

    def test_expiry_should_be_counted_from_now(self, private_key):
        now = datetime.now(timezone.utc).timestamp()
        assert claims(private_key)["exp"] == pytest.approx(now + 3600, abs=5)
