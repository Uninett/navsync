import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence, Union

import jwt
import pynetbox.core.api as netbox
from dynaconf import Dynaconf
from pynetbox.core.query import RequestError
from pynetbox.core.response import Record

from navsync import config
from navsync.parser import (
    Asset,
    Device,
    Location,
    NameStr,
    PhysicalChassis,
    SerialStr,
    Site,
    VirtualChassis,
    get_locations_from_devices,
    get_netbox_entities,
    get_sites_from_locations,
)
from navsync.utils import (
    NavServerInfo,
    init_logging,
    url_with_http,
    url_with_https,
)

_logger = logging.getLogger(__name__)


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
        chassis, devices = self._get_virtual_chassis_and_devices()
        locations = get_locations_from_devices(devices.values())
        sites = get_sites_from_locations(locations)
        assets = {device.name: device.asset for device in devices.values()}

        self.tags = self._get_or_create_tags(
            sites + locations + list(devices.values()) + list(assets.values())
        )
        self.upstream_device_type_by_part_number = self._get_upstream_device_types()
        self.tenants = self._get_upstream_tenants()

        self._sync_sites(sites)
        self._sync_locations(locations)
        self._sync_devices(devices)
        self._sync_assets(assets)

        self._sync_virtual_chassis(chassis.values())

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

    def _sync_virtual_chassis(self, chassis: Sequence[VirtualChassis]):
        upstream_chassis = self._get_upstream_chassis()
        upstream_devices = self._get_upstream_devices()
        for virtual_chassis in chassis:
            tag_ids = self._convert_tag_names_to_ids(virtual_chassis.tags, self.tags)
            upstream_virtual_chassis = upstream_chassis.get(virtual_chassis.name)
            if upstream_virtual_chassis:
                self._register_devices_as_members_of_vc(
                    virtual_chassis.devices, upstream_devices, upstream_virtual_chassis
                )
                if (
                    not upstream_virtual_chassis.description
                    and virtual_chassis.description
                ):
                    upstream_virtual_chassis.description = virtual_chassis.description
                if not upstream_virtual_chassis.comments and virtual_chassis.comments:
                    upstream_virtual_chassis.comments = virtual_chassis.comments
                if virtual_chassis.tenant:
                    upstream_virtual_chassis.tenant = virtual_chassis.tenant
                upstream_virtual_chassis_tag_ids = self._get_tag_ids_from_tags(
                    upstream_virtual_chassis.tags
                )
                tag_ids += [
                    tag_id
                    for tag_id in upstream_virtual_chassis_tag_ids
                    if tag_id not in tag_ids
                ]
                upstream_virtual_chassis.tags = tag_ids
                if upstream_virtual_chassis.updates():
                    _logger.debug(
                        f"Updating virtual chassis {upstream_virtual_chassis.name}"
                    )
                    upstream_virtual_chassis.save()
            else:
                new_virtual_chassis = {
                    "name": virtual_chassis.name,
                    "tenant": virtual_chassis.tenant,
                    "tags": tag_ids,
                }
                if virtual_chassis.description:
                    new_virtual_chassis["description"] = virtual_chassis.description
                if virtual_chassis.comments:
                    new_virtual_chassis["comments"] = virtual_chassis.comments
                _logger.debug(f"Creating new virtual chassis {virtual_chassis.name}")
                created_virtual_chassis = self.netbox_api.dcim.virtual_chassis.create(
                    **new_virtual_chassis
                )
                self._register_devices_as_members_of_vc(
                    virtual_chassis.devices, upstream_devices, created_virtual_chassis
                )

    def _sync_assets(self, assets: dict[NameStr, Asset]):
        upstream_devices_by_name = self._get_upstream_devices()
        upstream_assets_by_serial = self._get_upstream_assets()
        assets_by_serial = {
            asset.serial: asset for asset in assets.values() if asset.serial is not None
        }

        # Shelve any upstream assets that were registered by navsync
        # but was not found in the current sync
        for upstream_serial, upstream_asset in upstream_assets_by_serial.items():
            if "navsync" not in [tag.name for tag in upstream_asset.tags]:
                continue
            if upstream_asset.status != "used":
                continue
            if upstream_serial not in assets_by_serial:
                _logger.debug(f"Shelving asset {upstream_serial}")
                upstream_asset.status = "stored"
                upstream_asset.device = None
                # There is a problem where netbox sometimes returns a 500 error
                # when you save an asset even though the asset is saved successfully.
                try:
                    upstream_asset.save()
                except RequestError as e:
                    _logger.error(
                        f"Got error while updating asset {upstream_asset.serial}: {str(e)}"
                    )

        for device_name, asset in assets.items():
            if not asset.serial:
                continue
            upstream_asset = upstream_assets_by_serial.get(asset.serial)
            upstream_device = upstream_devices_by_name.get(device_name)
            if upstream_device is None:
                _logger.error(
                    f"When syncing asset {asset.serial}, could not find its device "
                    f"{device_name}. Skipping asset {asset.serial}"
                )
                continue
            tag_ids = self._convert_tag_names_to_ids(asset.tags, self.tags)

            prior_asset = self._get_upstream_asset_for_device(
                upstream_device.id, upstream_assets_by_serial.values()
            )
            if prior_asset and prior_asset.serial != asset.serial:
                _logger.debug(
                    f"Asset {prior_asset.serial} is currently assigned to device {upstream_device.name}. Unassigning it in favor of {asset.serial}."
                )
                prior_asset.device = None
                prior_asset.save()
                prior_asset.full_details()

            if upstream_asset:
                upstream_asset.device = upstream_device.id
                upstream_asset.device_type = upstream_device.device_type.id
                upstream_asset.owner = asset.owner
                upstream_asset.tenant = asset.tenant
                upstream_asset.status = asset.status
                if not upstream_asset.comments and asset.comments:
                    upstream_asset.comments = asset.comments
                if asset.contact is not None:
                    upstream_asset.contact = asset.contact
                upstream_asset_tag_ids = self._get_tag_ids_from_tags(
                    upstream_asset.tags
                )
                tag_ids += [
                    tag_id for tag_id in upstream_asset_tag_ids if tag_id not in tag_ids
                ]
                upstream_asset.tags = tag_ids
                if upstream_asset.updates():
                    # There is a problem where netbox sometimes returns a 500 error
                    # when you save an asset even though the asset is saved successfully.
                    _logger.debug(f"Updating asset {upstream_asset.serial}")
                    try:
                        upstream_asset.save()
                    except RequestError as e:
                        _logger.error(
                            f"Got error while updating asset {upstream_asset.serial}: {str(e)}"
                        )
            else:
                new_asset_dict = {
                    "serial": asset.serial,
                    "status": asset.status,
                    "owner": asset.owner,
                    "tags": tag_ids,
                    "tenant": asset.tenant,
                    "device": upstream_device.id,
                    "device_type": upstream_device.device_type.id,
                }

                _logger.debug(f"Creating new asset {asset.serial}")
                # There is a problem where netbox sometimes returns a 500 error
                # when you create an asset even though the asset is created successfully.
                try:
                    self.netbox_api.plugins.inventory.assets.create(**new_asset_dict)
                except RequestError as e:
                    _logger.error(
                        f"Got error while creating asset {asset.serial}: {str(e)}"
                    )

    def _get_upstream_asset_for_device(
        self, device_id: int, upstream_assets: Sequence[Record]
    ) -> Optional[Record]:
        for asset in upstream_assets:
            # Can either be None, an int int or a Record object
            if asset.device is None:
                continue
            elif isinstance(asset.device, int):
                asset_device_id = asset.device
            else:
                asset_device_id = asset.device.id
            if asset_device_id == device_id:
                return asset
        return None

    def _sync_devices(self, devices: dict[NameStr, Device]):
        upstream_devices_by_name = self._get_upstream_devices()
        upstream_sites = self._get_upstream_sites()
        upstream_locations_by_name = self._get_upstream_locations()
        upstream_device_roles_by_name = self._get_upstream_device_roles()

        # Shelve any upstream devices that were registered by navsync
        # but was not found in the current sync
        for upstream_device in upstream_devices_by_name.values():
            if "navsync" not in [tag.name for tag in upstream_device.tags]:
                continue
            if upstream_device.status.value != "active":
                continue
            if upstream_device.name not in devices:
                _logger.debug(f"Decommissioning device {upstream_device.name}")
                upstream_device.status = "inventory"
                upstream_device.location = None
                upstream_device.tenant = None
                upstream_device.virtual_chassis = None
                upstream_device.vc_position = None
                upstream_device.save()

        for device in devices.values():
            upstream_device = upstream_devices_by_name.get(device.name)
            tag_ids = self._convert_tag_names_to_ids(device.tags, self.tags)
            if upstream_device:
                upstream_device.tenant = device.tenant

                upstream_device_tag_ids = self._get_tag_ids_from_tags(
                    upstream_device.tags
                )
                tag_ids += [
                    tag_id
                    for tag_id in upstream_device_tag_ids
                    if tag_id not in tag_ids
                ]

                upstream_device.tags = tag_ids
                upstream_device.status = "active"
                upstream_site = self._get_upstream_site(
                    upstream_sites, device.location.site
                )
                if not upstream_site:
                    raise ValueError(
                        f"Could not find site {device.location.site.name}. It should have been created during `_sync_sites`"
                    )
                upstream_device.site = upstream_site.id

                upstream_location = upstream_locations_by_name.get(device.location.name)
                if upstream_location:
                    upstream_device.location = upstream_location.id

                upstream_device_role = upstream_device_roles_by_name.get(device.role)
                if not upstream_device_role:
                    raise ValueError(f"Could not find device_role {device.role}")
                upstream_device.role = upstream_device_role.id

                upstream_device_type = self.upstream_device_type_by_part_number.get(
                    device.model
                )
                if not upstream_device_type:
                    _logger.error(
                        f"Could not find device_type for model {device.model}. Not updating device_type for device {upstream_device.name}"
                    )
                else:
                    upstream_device.device_type = upstream_device_type.id

                if upstream_device.updates():
                    _logger.debug(f"Updating device {upstream_device.name}")
                    upstream_device.save()
            else:
                upstream_site = self._get_upstream_site(
                    upstream_sites, device.location.site
                )
                if not upstream_site:
                    raise ValueError(
                        f"Could not find site {device.location.site.name}. It should have been created during `_sync_sites`"
                    )
                upstream_device_type = self.upstream_device_type_by_part_number.get(
                    device.model
                )
                if not upstream_device_type:
                    _logger.error(
                        f"Could not find device_type for model {device.model}. Skipping device {device.name}"
                    )
                    continue
                upstream_device_role = upstream_device_roles_by_name.get(device.role)
                if not upstream_device_type:
                    raise ValueError(f"Could not find device_role {device.role}")
                new_device_dict = {
                    "name": device.name,
                    "device_type": upstream_device_type.id,
                    "role": upstream_device_role.id,
                    "site": upstream_site.id,
                    "tags": tag_ids,
                    "tenant": device.tenant,
                }
                if upstream_location := upstream_locations_by_name.get(
                    device.location.name
                ):
                    new_device_dict["location"] = upstream_location.id

                _logger.debug(f"Creating new device {device.name}")
                self.netbox_api.dcim.devices.create(**new_device_dict)

    def _sync_locations(self, locations: Sequence[Location]):
        upstream_sites = self._get_upstream_sites()
        upstream_locations_by_name = self._get_upstream_locations()

        for location in locations:
            upstream_location = upstream_locations_by_name.get(location.name)
            tag_ids = self._convert_tag_names_to_ids(location.tags, self.tags)
            if upstream_location:
                upstream_location.tenant = location.tenant
                if not upstream_location.description and location.description:
                    upstream_location.description = location.description

                upstream_location_tag_ids = self._get_tag_ids_from_tags(
                    upstream_location.tags
                )
                tag_ids += [
                    tag_id
                    for tag_id in upstream_location_tag_ids
                    if tag_id not in tag_ids
                ]

                upstream_location.tags = tag_ids
                upstream_location.status = location.status
                upstream_site = self._get_upstream_site(upstream_sites, location.site)
                if not upstream_site:
                    raise ValueError(
                        f"Could not find site {location.site.name}. It should have been created during `_sync_sites`"
                    )
                upstream_location.site = upstream_site.id
                if upstream_location.updates():
                    _logger.debug(f"Updating location {upstream_location.name}")
                    upstream_location.save()
            else:
                upstream_site = self._get_upstream_site(upstream_sites, location.site)
                if not upstream_site:
                    raise ValueError(
                        f"Could not find site {location.site.name}. It should have been created during `_sync_sites`"
                    )
                new_location_dict = {
                    "site": upstream_site.id,
                    "name": location.name,
                    "slug": "-".join(location.name.split()).lower(),
                    "status": location.status,
                    "tags": tag_ids,
                    "tenant": location.tenant,
                }
                if location.description:
                    new_location_dict["description"] = location.description
                _logger.debug(f"Creating new location {location.name}")
                self.netbox_api.dcim.locations.create(**new_location_dict)

    def _sync_sites(self, sites: Sequence[Site]):
        upstream_sites = self._get_upstream_sites()
        for site in sites:
            upstream_site = self._get_upstream_site(upstream_sites, site)
            if upstream_site:
                self._update_site(site, upstream_site)
            else:
                self._create_site(site)

    def _update_site(self, site: Site, upstream_site: Record):
        if site.latitude and site.longitude:
            if upstream_site.latitude and upstream_site.longitude:
                # upstream_site.updates() always detects changes in lat/long even
                # if there are none, so we have to set them conditionally
                if (
                    upstream_site.latitude - site.latitude > 1e-6
                    or upstream_site.longitude - site.longitude > 1e-6
                ):
                    upstream_site.latitude = f"{site.latitude:.6f}"
                    upstream_site.longitude = f"{site.longitude:.6f}"
            else:
                upstream_site.latitude = f"{site.latitude:.6f}"
                upstream_site.longitude = f"{site.longitude:.6f}"

        upstream_site.tenant = site.tenant
        if not upstream_site.description and site.description:
            upstream_site.description = site.description
        if not upstream_site.comments and site.comments:
            upstream_site.comments = site.comments
        if site.physical_address is not None:
            upstream_site.physical_address = site.physical_address
        if site.region is not None:
            upstream_site.region = site.region

        tag_ids = self._convert_tag_names_to_ids(site.tags, self.tags)
        upstream_site_tag_ids = self._get_tag_ids_from_tags(upstream_site.tags)
        tag_ids += [tag_id for tag_id in upstream_site_tag_ids if tag_id not in tag_ids]

        upstream_site.tags = tag_ids
        upstream_site.status = site.status
        if upstream_site.updates():
            _logger.debug(f"Updating site {upstream_site.name}")
            upstream_site.save()

    def _create_site(self, site: Site):
        tag_ids = self._convert_tag_names_to_ids(site.tags, self.tags)
        new_site_dict = {
            "tenant": site.tenant,
            "status": site.status,
            "name": site.name,
            "slug": site.slug,
            "tags": tag_ids,
        }
        if site.latitude and site.longitude:
            new_site_dict["latitude"] = f"{site.latitude:.6f}"
            new_site_dict["longitude"] = f"{site.longitude:.6f}"
        if site.physical_address:
            new_site_dict["physical_address"] = site.physical_address
        if site.comments:
            new_site_dict["comments"] = site.comments
        if site.description:
            new_site_dict["description"] = site.description
        if site.region:
            new_site_dict["region"] = site.region
        _logger.debug(f"Creating new site {site.name}")
        self.netbox_api.dcim.sites.create(**new_site_dict)

    def _convert_tag_names_to_ids(
        self, tag_names: list[NameStr], all_tags: dict[NameStr, int]
    ) -> list[int]:
        """Converts a list of tag names to a list of tag IDs using all_tags as the source of IDs
        Any duplicate tag names will be ignored. Tag names not found in all_tags will be ignored.
        """
        tag_ids = set(all_tags[tag] for tag in tag_names if tag in all_tags)
        return list(tag_ids)

    def _get_or_create_tags(
        self, objects: list[Union[Site, Location, Device, Asset, VirtualChassis]]
    ) -> dict[NameStr, int]:
        """Returns dict mapping tag names to their IDs. Creates any tags that do not already exist."""
        existing_tags = {tag.name: tag.id for tag in self.netbox_api.extras.tags.all()}
        for obj in objects:
            for tag_name in obj.tags:
                if tag_name not in existing_tags:
                    created_tag = self.netbox_api.extras.tags.create(
                        name=tag_name, slug=tag_name
                    )
                    existing_tags[created_tag.name] = created_tag.id
        return existing_tags

    def _get_upstream_chassis(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to virtual chassis"""
        return {
            chassis.name: chassis
            for chassis in self.netbox_api.dcim.virtual_chassis.all()
        }

    def _get_upstream_assets(self) -> dict[SerialStr, Record]:
        """Returns dict mapping serial number to asset"""
        return {
            asset.serial: asset
            for asset in self.netbox_api.plugins.inventory.assets.all()
        }

    def _get_upstream_tenants(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to tenant"""
        return {tenant.name: tenant for tenant in self.netbox_api.tenancy.tenants.all()}

    def _get_upstream_device_types(self) -> dict[str, Record]:
        """Returns dict mapping part number to device type"""
        return {
            device_type.part_number: device_type
            for device_type in self.netbox_api.dcim.device_types.all()
        }

    def _get_upstream_device_roles(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to device role"""
        return {
            device_role.name: device_role
            for device_role in self.netbox_api.dcim.device_roles.all()
        }

    def _get_upstream_locations(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to location"""
        return {
            location.name: location for location in self.netbox_api.dcim.locations.all()
        }

    def _get_upstream_devices(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to device"""
        return {device.name: device for device in self.netbox_api.dcim.devices.all()}

    def _get_upstream_sites(self) -> list[Record]:
        return list(self.netbox_api.dcim.sites.all())

    def _get_upstream_site(
        self, upstream_sites: Sequence[Record], site: Site
    ) -> Optional[Record]:
        """Looks through sequence of upstream sites to find matching site.
        Returns upstream site of type `Record` if there is a match.
        Returns None if there is no match
        """
        for upstream_site in upstream_sites:
            if (
                upstream_site.slug == site.slug
                or upstream_site.physical_address == site.physical_address
            ):
                return upstream_site

    def _register_devices_as_members_of_vc(
        self,
        devices: Sequence[Device],
        upstream_devices: dict[NameStr, Record],
        virtual_chassis: Record,
    ):
        for device in devices:
            if upstream_device := upstream_devices.get(device.name):
                if device.vc_position is None:
                    _logger.error(
                        f"Device {device.name} is missing position, cannot register as member of a virtual chassis"
                    )
                    continue
                if (
                    upstream_device.virtual_chassis
                    and upstream_device.virtual_chassis.id == virtual_chassis.id
                    and upstream_device.vc_position == device.vc_position
                ):
                    # Device is already registered in correct position
                    continue

                prior_stack_member = self._get_device_for_pos_in_vc(
                    virtual_chassis.id,
                    upstream_devices,
                    device.vc_position,
                )

                if prior_stack_member:
                    prior_stack_member.vc_position = None
                    prior_stack_member.virtual_chassis = None
                    try:
                        prior_stack_member.save()
                        # Needs to be done to reset cached values for if fields have been modified
                        prior_stack_member.full_details()
                        _logger.debug(
                            f"Unregistering device {prior_stack_member.name} from virtual chassis {virtual_chassis.name}"
                        )
                    except RequestError as e:
                        _logger.error(
                            f"Could not unregister device {prior_stack_member.name} from virtual chassis {virtual_chassis.name}: {str(e)}"
                        )

                upstream_device.virtual_chassis = virtual_chassis.id
                upstream_device.vc_position = device.vc_position
                try:
                    upstream_device.save()
                    # Needs to be done to reset cached values for if fields have been modified
                    upstream_device.full_details()
                    _logger.debug(
                        f"Registering device {upstream_device.name} as part of virtual chassis {virtual_chassis.name} in position {upstream_device.vc_position}"
                    )
                except RequestError as e:
                    _logger.error(
                        f"Could not register device {upstream_device.name} as part of virtual chassis {virtual_chassis.name} in position {upstream_device.vc_position}: {str(e)}"
                    )

    def _get_device_for_pos_in_vc(
        self,
        virtual_chassis_id: int,
        upstream_devices: dict[NameStr, Record],
        position: int,
    ) -> Optional[Record]:
        """Returns the device in the given position in the given virtual chassis.
        If no such device exists, returns None.
        """
        for device in upstream_devices.values():
            if device.virtual_chassis:
                # it will be a Record straight from Netbox, but will be int if its been modified locally
                if isinstance(device.virtual_chassis, int):
                    upstream_virtual_chassis_id = device.virtual_chassis
                else:
                    upstream_virtual_chassis_id = device.virtual_chassis.id

                if (
                    upstream_virtual_chassis_id == virtual_chassis_id
                    and device.vc_position == position
                ):
                    return device
        return None

    def _get_virtual_chassis_and_devices(
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
                        _logger.error(
                            f"Duplicate virtual chassis name {entity.name} found in NAV server {nav_server.url}. Dropping duplicate."
                        )
                        continue
                    virtual_chassises[entity.name] = entity
                    for device in entity.devices:
                        if device.name in devices:
                            _logger.error(
                                f"Duplicate device name {device.name} found in NAV server {nav_server.url}. Dropping duplicate."
                            )
                            continue
                        devices[device.name] = device
                case PhysicalChassis():
                    if entity.name in devices:
                        _logger.error(
                            f"Duplicate physical chassis name {entity.name} found in NAV server {nav_server.url}. Dropping duplicate."
                        )
                        continue
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
        test_vms = self.netbox_api.virtualization.virtual_machines.filter(
            role="testverktykasse", status="active"
        )
        test_devices = self.netbox_api.dcim.devices.filter(
            role="testverktykasse", status="active"
        )
        test_vm_ids = {vm.id for vm in test_vms}
        test_device_ids = {device.id for device in test_devices}
        for vm in virtual_machines:
            if vm.id in test_vm_ids:
                _logger.debug(f"Skipping test VM {vm.name}")
                continue
            if "owner" not in vm.custom_fields:
                _logger.error(
                    f"VM {vm.name} is missing custom field 'owner'. Cannot determine NAV server owner. Skipping."
                )
                continue
            if "id" not in vm.custom_fields["owner"]:
                _logger.error(
                    f"VM {vm.name} has invalid value for custom field 'owner'. Cannot determine NAV server owner. Skipping."
                )
                continue
            yield NavServerInfo(
                id=vm.id,
                url=self._get_url_from_name(vm.name),
                owner_id=vm.custom_fields["owner"]["id"],
                tenant_id=vm.tenant.id,
            )

        for device in devices:
            if device.id in test_device_ids:
                _logger.debug(f"Skipping test device {device.name}")
                continue
            asset = self.netbox_api.plugins.inventory.assets.get(device=device)
            if not asset:
                _logger.error(
                    f"Device {device.name} has no asset assigned. Cannot determine NAV server owner. Skipping."
                )
                continue
            if not hasattr(device, "tenant") or device.tenant is None:
                _logger.error(
                    f"Device {device.name} has no tenant assigned. Cannot determine NAV server tenant. Skipping."
                )
                continue
            if not hasattr(asset, "owner") or asset.owner is None:
                _logger.error(
                    f"Device {device.name}'s asset has no owner assigned. Cannot determine NAV server owner. Skipping."
                )
                continue
            yield NavServerInfo(
                id=device.id,
                url=self._get_url_from_name(device.name),
                owner_id=asset.owner.id,
                tenant_id=device.tenant.id,
            )

    def _get_url_from_name(self, device_name: str) -> str:
        if self.https:
            return url_with_https(device_name)
        else:
            return url_with_http(device_name)

    def _get_tag_ids_from_tags(self, tags: list[Union[int, Record]]) -> list[int]:
        tag_ids = []
        for tag in tags:
            if isinstance(tag, int):
                tag_id = tag
            else:
                tag_id = tag.id
            if tag_id not in tag_ids:
                tag_ids.append(tag_id)
        return tag_ids


if __name__ == "__main__":
    main()
