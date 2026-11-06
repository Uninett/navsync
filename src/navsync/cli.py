"""Command line entry point for navsync."""

import argparse

from navsync import config
from navsync.syncer import Syncer
from navsync.utils import init_logging

EXAMPLE_CONFIG = """\
[netbox]
# URL of the Netbox instance to sync into.
url = "http://127.0.0.1:8080"

# An API token with read/write access to the Netbox instance's 'DCIM', 'IPAM',
# 'Plugins (Inventory)', 'Virtualization', 'Tenancy' and 'Extras' API endpoints.
token = "0123456789"

[nav]
# Path to a PEM-encoded RSA private key used to sign the JWTs that authenticate
# navsync towards the NAV API. The corresponding public key must be registered
# with each NAV instance to be accessed.
private_key_path = "/path/to/private_key.pem"

# How long (in seconds) a generated JWT should be valid for.
expiry_delta = 3600

# Sets the 'iss' claim of generated tokens. Must match the value configured in
# the NAV instances being synced against.
issuer = "navsync"

# If true, calls to the NAV APIs use https://, otherwise http://.
https = true

# Optional. Only NAV netboxes in these categories are synced. Defaults to all
# categories.
categories = ["GW", "GSW", "SW", "EDGE", "WLAN", "SRV", "OTHER", "ENV", "POWER"]

[geocode]
# Optional. If true, geocoding results are cached on disk so repeated runs don't
# query Kartverket again for the same address. Defaults to false.
cache_enabled = false

# Optional. File used to cache geocoding results between runs. Defaults to
# `geocode.json` in the user cache directory, e.g. ~/.cache/navsync/geocode.json.
# The user running navsync must be able to write to it.
cache_path = "~/.cache/navsync/geocode.json"

# Optional. How long (in seconds) a geocoding result is cached, including when
# an address was not found or was ambiguous. Defaults to 30 days.
cache_ttl = 2592000

# Optional. User-Agent header sent with requests to Kartverket, so they can
# identify who is calling their API. Defaults to "navsync". You should probably
# include a contact email address, e.g. "navsync (you@example.org)", so they can
# reach you if your usage causes problems.
user_agent = "navsync"
"""


def main():
    args = parse_args()
    init_logging(args.loglevel)
    settings = config.settings
    settings.validators.validate(only=["nav"])
    syncer = Syncer.from_settings(settings, nosync=args.nosync)
    syncer.sync()


def parse_args():
    description = (
        "Syncs navboxes (a.k.a. netboxes in NAV) from NAV to Netbox. "
        "Configuration is needed prior to running this script. "
        "Configuration should be placed at "
        "'$CONFDIR/navsync/navsync.toml', where $CONFDIR is your "
        "system's default configuration directory, e.g. '~/.config'.\n\n"
        "Example configuration\n"
        "---------------------\n"
        f"{EXAMPLE_CONFIG}"
        "---------------------\n"
    )
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(formatter_class=formatter, description=description)
    parser.add_argument(
        "--loglevel",
        action="store",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="The lowest severity level a message being logged can have",
        default="WARNING",
    )
    parser.add_argument(
        "--nosync",
        action="store_true",
        help="Do not sync navboxes from NAV to Netbox, only get data from NAV instances. Useful "
        "for testing connectivity and permissions towards NAV instances without making any changes in Netbox",
    )
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    main()
