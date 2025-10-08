import argparse
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import jwt
import pynetbox.core.api as netbox
from dynaconf import Dynaconf

from navsync import config
from navsync.parser import (
    Device,
    NameStr,
    PhysicalChassis,
    VirtualChassis,
    get_netbox_entities,
)
from navsync.utils import NavServerInfo, init_logging, url_with_http, url_with_https


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
    init_logging(args.loglevel)
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

    def _get_virtual_chassises_and_devices(
        self,
    ) -> tuple[dict[NameStr, VirtualChassis], dict[NameStr, Device]]:
        entities = []
        for nav_server in self._get_nav_servers():
            token = self._generate_nav_token(aud=nav_server.url)
            # Sleep to avoid issues with the `nbf` claim.
            time.sleep(1)
            entities.extend(get_netbox_entities(nav_server, token))

        virtual_chassises: dict[NameStr, VirtualChassis] = {}
        devices: dict[NameStr, Device] = {}

        for entity in entities:
            match entity:
                case VirtualChassis():
                    if entity.name in virtual_chassises:
                        raise ValueError(
                            f"Duplicate virtual chassis name {entity.name} found in NAV server {nav_server.url}"
                        )
                    virtual_chassises[entity.name] = entity
                    for device in entity.devices:
                        if device.name in devices:
                            raise ValueError(
                                f"Duplicate device name {device.name} found in NAV server {nav_server.url}"
                            )
                        devices[device.name] = device
                case PhysicalChassis():
                    if entity.name in devices:
                        raise ValueError(
                            f"Duplicate device name {entity.name} found in NAV server {nav_server.url}"
                        )
                    devices[entity.name] = entity
                case _:
                    raise TypeError(f"Unexpected entity type {type(entity)}")

        return virtual_chassises, devices

    def _generate_nav_token(self, aud: str):
        now = datetime.now(timezone.utc)
        jwt_claims = {
            "exp": (now + self.expiry_delta).timestamp(),
            "nbf": now.timestamp(),
            "iat": now.timestamp(),
            "aud": aud,
            "iss": self.issuer,
            "token_type": "access",
            "endpoints": ["/api/1/netbox", "/api/1/netboxentity", "/api/1/location"],
            "write": False,
        }
        return jwt.encode(jwt_claims, self.private_key, algorithm="RS256")

    def _get_nav_servers(self) -> Iterable[NavServerInfo]:
        """
        For each NAV server instance found on the Netbox server, yields a
        namespace containing that instance's url, owner, and tenant
        """
        virtual_machines = self.netbox_api.virtualization.virtual_machines.filter(
            role="verktykasse", status="active"
        )
        devices = self.netbox_api.dcim.devices.filter(
            role="verktykasse", status="active"
        )

        for vm in virtual_machines:
            if "owner" not in vm.custom_fields:
                self.log_netbox_insufficiency(
                    None, vm, "custom_fields", "Missing 'owner'"
                )
                continue
            if not hasattr(vm.custom_fields["owner"], "id"):
                self.log_netbox_insufficiency(
                    None, vm, "custom_fields", "'owner' should be a Tenant with an 'id'"
                )
                continue
            yield NavServerInfo(
                url=self.get_url_from_name(vm.name),
                owner_id=vm.custom_fields["owner"].id,
                tenant_id=vm.tenant.id,
            )

        for device in devices:
            asset = self.netbox_api.plugins.inventory.assets.get(device=device)
            if not asset:
                self.log_netbox_insufficiency(
                    None, device, None, "No asset assigned to device"
                )
                continue
            if not hasattr(asset, "tenant") or asset.tenant is None:
                self.log_netbox_insufficiency(None, asset, "tenant", "Missing tenant")
                continue
            yield NavServerInfo(
                url=self._get_url_from_name(device.name),
                owner_id=asset.owner.id,
                tenant_id=asset.tenant.id,
            )

    def _get_url_from_name(self, device_name: str) -> str:
        if self.https:
            return url_with_https(device_name)
        else:
            return url_with_http(device_name)


if __name__ == "__main__":
    main()
