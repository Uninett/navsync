import argparse
from datetime import timedelta

import pynetbox.core.api as netbox
from dynaconf import Dynaconf

from navsync import config, utils

EXAMPLE_CONFIG = """\
[netbox]
# The 'url' option specifies the URL of the Netbox instance for which navboxes
# from NAV should be synced to.
url="http://127.0.0.1:8080"

# The 'token' option specifies an API token with read/write access to the Netbox
# instance's 'DCIM', 'Plugins (Inventory)', 'Virtualization' and 'Tenancy' API
# endpoints
token="0123456789"

[nav]
# NAV instances to sync navboxes from are found by looking through
# - the 'Device' entries in the Netbox instance, for any device with
#   Role='Verktøykasse' AND Status='Active'
# - the 'VM' entries in the Netbox instance, for any VM with Role='Verktøykasse'
#   AND Status='Active'

# The 'private_key_path' option specifies a path to a PEM-encoded RSA private key
# that will be used to sign JWTs used for authentication towards the NAV API.
# The corresponding public key must be registered with the NAV instance(s) to be accessed.
private_key_path="/tmp/private_key.pem"

# the 'expiry_delta' option specifies how long (in seconds) a JWT
# should be valid for.
expiry_delta=3600

# `issuer` is used to set the `iss` claim for generated tokens. This must match
# the value configured in the NAV instances you are syncing against
issuer="netbox-tools"\

# If `https` is true, calls to the NAV APIs will use https://
# If false, it will use http://
https=true
"""


def main():
    args = parse_args()
    utils.init_logging(args.loglevel)
    settings = config.settings
    settings.validators.validate(only=["nav"])
    syncer = Syncer.from_settings(settings, args)
    syncer.sync()


def parse_args():
    description = (
        "Syncs navboxes (a.k.a. netboxes in NAV) from NAV to Netbox. "
        "Configuration is needed prior to running this script. "
        "Configuration should be placed at "
        "'$CONFDIR/netbox-tools/netbox-tools.toml', where $CONFDIR is your "
        "system's default configuration directory, e.g. '~/.config'.\n\n"
        "Example minimal configuration\n"
        "-----------------------------\n"
        f"{EXAMPLE_CONFIG}\n"
        "-----------------------------\n"
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
    args = parser.parse_args()
    return args


class Syncer:
    """
    Performs all the logic needed to sync models from NAV to Netbox.

    Should be instantiated using :meth from_settings:
    Use :meth sync: to sync
    """

    private_key: str
    expiry_delta: timedelta
    issuer: str
    netbox_api: netbox.Api
    https: bool

    def __init__(
        self,
        netbox_api: netbox.Api,
        private_key: str,
        expiry_delta: timedelta,
        issuer: str,
        https: bool,
    ):
        self.netbox_api = netbox_api
        self.private_key = private_key
        self.expiry_delta = expiry_delta
        self.issuer = issuer
        self.https = https

    def sync(self):
        """
        Syncs all navboxes from all NAV server instances found on the Netbox
        server to Netbox
        """
        raise NotImplementedError()

    @classmethod
    def from_settings(cls, settings: Dynaconf, args: argparse.Namespace):
        """
        Initialize a syncer based on user-supplied settings

        :param settings: the netbox-tools config-file, already parsed and validated
        """
        netbox_token = settings.netbox.token
        netbox_url = settings.netbox.url

        expiry_delta = timedelta(seconds=settings.nav.expiry_delta)
        nav_iss_claim = settings.nav.issuer
        https = settings.nav.https
        with open(settings.nav.private_key_path, "r") as f:
            private_key = f.read()

        return cls(
            netbox_api=netbox.Api(url=netbox_url, token=netbox_token),
            private_key=private_key,
            expiry_delta=expiry_delta,
            issuer=nav_iss_claim,
            https=https,
        )


if __name__ == "__main__":
    main()
