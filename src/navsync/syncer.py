import argparse
import logging
import time
from dataclasses import dataclass, field
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
    EntityParser,
    Location,
    LocationHierarchyParser,
    ManufacturerStr,
    ModelStr,
    NameStr,
    SerialStr,
    Site,
    SlugStr,
    VirtualChassis,
)
from navsync.utils import (
    NavServerInfo,
    init_logging,
    sanitize_slug,
    url_with_http,
    url_with_https,
)

_logger = logging.getLogger(__name__)


@dataclass
class NavData:
    sites: list[Site] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    chassis: dict[NameStr, VirtualChassis] = field(default_factory=dict)
    devices: dict[NameStr, Device] = field(default_factory=dict)


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
    parser.add_argument(
        "--nosync",
        action="store_true",
        help="Do not sync navboxes from NAV to Netbox, only get data from NAV instances. Useful "
        "for testing connectivity and permissions towards NAV instances without making any changes in Netbox",
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
    nosync: bool

    def __init__(
        self,
        netbox_api: netbox.Api,
        private_key: str,
        expiry_delta: timedelta,
        issuer: str,
        https: bool,
        nosync: bool,
    ):
        self.netbox_api = netbox_api
        self.private_key = private_key
        self.expiry_delta = expiry_delta
        self.issuer = issuer
        self.https = https
        self.nosync = nosync

    def sync(self):
        """
        Syncs all navboxes from all NAV server instances found on the Netbox
        server to Netbox
        """
        self.tenants = self._get_upstream_tenants()
        nav_data = self._fetch_nav_data()
        assets = {
            device.name: device.asset
            for device in nav_data.devices.values()
            if device.asset is not None
        }

        if self.nosync:
            _logger.info("Nosync flag is set, not syncing to Netbox")
            return

        self.tags = self._get_or_create_tags(
            nav_data.sites
            + nav_data.locations
            + list(nav_data.devices.values())
            + list(assets.values())
        )
        self.upstream_device_types_by_part_number = (
            self._get_upstream_device_types_by_part_number()
        )
        self.upstream_device_types_by_model = self._get_upstream_device_types_by_model()
        self.upstream_manufacturers = self._get_upstream_manufacturers()

        flat_locations = self._sync_sites_and_locations(nav_data.sites)
        self._sync_devices(nav_data.devices, flat_locations)
        self._sync_assets(assets)

        self._sync_virtual_chassis(nav_data.chassis)

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
            nosync=args.nosync,
        )

    def _sync_virtual_chassis(self, chassis: dict[NameStr, VirtualChassis]):
        upstream_chassis = self._get_upstream_chassis()
        upstream_devices = self._get_upstream_devices()

        self._delete_all_missing_virtual_chassis(
            chassis, upstream_chassis, upstream_devices.values()
        )
        for virtual_chassis in chassis.values():
            upstream_virtual_chassis = upstream_chassis.get(virtual_chassis.name)
            if upstream_virtual_chassis:
                self._update_virtual_chassis(
                    virtual_chassis, upstream_virtual_chassis, upstream_devices
                )
            else:
                self._create_virtual_chassis(virtual_chassis, upstream_devices)

    def _delete_all_missing_virtual_chassis(
        self,
        chassis: dict[NameStr, VirtualChassis],
        upstream_chassis: dict[NameStr, Record],
        upstream_devices: Sequence[Record],
    ):
        """Deletes any upstream virtual chassis that were registered by navsync but were not found in the current sync"""
        for upstream_name, upstream_vc in upstream_chassis.items():
            if upstream_name in chassis:
                continue
            tags = [tag.name for tag in upstream_vc.tags]
            if "navsync" not in tags or "cnaas" not in tags:
                continue
            if (
                hasattr(upstream_vc, "role")
                and upstream_vc.role.slug == "verktykassecnaas"
            ):
                continue
            try:
                self._unregister_devices_as_members_of_vc(upstream_vc, upstream_devices)
            except RequestError as e:
                _logger.error(
                    f"Failed to unregister devices from virtual chassis {upstream_vc.name}: {e}"
                )
                return
            _logger.debug(f"Deleting virtual chassis {upstream_vc.name}")
            try:
                upstream_vc.delete()
            except RequestError as e:
                _logger.error(
                    f"Failed to delete virtual chassis {upstream_vc.name}: {e}"
                )

    def _unregister_devices_as_members_of_vc(
        self, virtual_chassis: VirtualChassis, upstream_devices: Sequence[Record]
    ):
        """Unregisters all members of `virtual_chassis`. Uses `upstream_devices` to find the members and update them accordingly."""
        for upstream_device in upstream_devices:
            if (
                upstream_device.virtual_chassis
                and upstream_device.virtual_chassis.id == virtual_chassis.id
            ):
                _logger.debug(
                    f"Unregistering device {upstream_device.name} from virtual chassis {virtual_chassis.name}"
                )
                upstream_device.virtual_chassis.id = None
                upstream_device.vc_position = None
                upstream_device.save()
                upstream_device.full_details()

    def _create_virtual_chassis(
        self, virtual_chassis: VirtualChassis, upstream_devices: dict[NameStr, Record]
    ):
        tag_ids = self._convert_tag_names_to_ids(
            virtual_chassis.tags + ["cnaas"], self.tags
        )
        new_virtual_chassis = {
            "name": virtual_chassis.name,
            "tags": tag_ids,
        }
        _logger.debug(f"Creating new virtual chassis {virtual_chassis.name}")
        try:
            created_virtual_chassis = self.netbox_api.dcim.virtual_chassis.create(
                **new_virtual_chassis
            )
        except RequestError as e:
            _logger.error(
                f"Failed to create virtual chassis {virtual_chassis.name}: {e}"
            )
            return
        self._register_devices_as_members_of_vc(
            virtual_chassis.devices, upstream_devices, created_virtual_chassis
        )

    def _update_virtual_chassis(
        self,
        virtual_chassis: VirtualChassis,
        upstream_virtual_chassis: Record,
        upstream_devices: dict[NameStr, Record],
    ):
        self._register_devices_as_members_of_vc(
            virtual_chassis.devices, upstream_devices, upstream_virtual_chassis
        )
        tag_ids = self._convert_tag_names_to_ids(virtual_chassis.tags, self.tags)
        upstream_virtual_chassis_tag_ids = self._get_tag_ids_from_tags(
            upstream_virtual_chassis.tags
        )
        tag_ids += [
            tag_id
            for tag_id in upstream_virtual_chassis_tag_ids
            if tag_id not in tag_ids
        ]
        tag_ids.sort()
        upstream_virtual_chassis.tags = tag_ids
        if upstream_virtual_chassis.updates():
            _logger.debug(f"Updating virtual chassis {upstream_virtual_chassis.name}")
            try:
                upstream_virtual_chassis.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to update virtual chassis {upstream_virtual_chassis.name}: {e}"
                )

    def _sync_assets(self, assets: dict[NameStr, Asset]):
        upstream_devices_by_name = self._get_upstream_devices()
        upstream_assets = self._get_upstream_assets()
        upstream_asset_list = [
            asset
            for device_assets in upstream_assets.values()
            for asset in device_assets.values()
        ]

        self._shelve_all_missing_assets(upstream_assets, assets.values())

        for device_name, asset in assets.items():
            upstream_device = upstream_devices_by_name.get(device_name)
            if upstream_device is None:
                _logger.error(
                    f"When syncing asset {asset.serial}, could not find its device "
                    f"{device_name}. Skipping asset {asset.serial}"
                )
                continue
            upstream_asset = upstream_assets.get(
                upstream_device.device_type.id, {}
            ).get(asset.serial)

            prior_asset = self._get_upstream_asset_for_device(
                upstream_device.id, upstream_asset_list
            )
            if prior_asset and prior_asset.serial != asset.serial:
                _logger.debug(
                    f"Asset {prior_asset.serial} is currently assigned to device {upstream_device.name}. Unassigning it in favor of {asset.serial}."
                )
                prior_asset.device = None
                prior_asset.save()
                prior_asset.full_details()

            if upstream_asset:
                self._update_asset(asset, upstream_asset, upstream_device)
            else:
                self._create_asset(asset, upstream_device)

    def _create_asset(self, asset: Asset, upstream_device: Record):
        tag_ids = self._convert_tag_names_to_ids(asset.tags + ["cnaas"], self.tags)
        new_asset_dict = {
            "serial": asset.serial,
            "status": asset.status,
            "owner": asset.owner,
            "tags": tag_ids,
            "tenant": asset.tenant,
            "device": upstream_device.id,
            "device_type": upstream_device.device_type.id,
            "custom_fields": {"software_version": asset.software_version},
        }

        _logger.debug(f"Creating new asset {asset.serial}")
        # There is a problem where netbox sometimes returns a 500 error
        # when you create an asset even though the asset is created successfully.
        try:
            self.netbox_api.plugins.inventory.assets.create(**new_asset_dict)
        except RequestError as e:
            _logger.error(f"Failed to create asset {asset.serial}: {str(e)}")

    def _update_asset(
        self, asset: Asset, upstream_asset: Record, upstream_device: Record
    ):
        upstream_asset.device = upstream_device.id
        upstream_asset.owner = asset.owner
        upstream_asset.tenant = asset.tenant
        upstream_asset.status = asset.status
        if asset.contact is not None:
            upstream_asset.contact = asset.contact
        if asset.software_version:
            # Merge into the existing dict so _diff() compares equal dicts when the value
            # hasn't changed. Replacing with a subset dict triggers a spurious update.
            upstream_asset.custom_fields["software_version"] = asset.software_version
        tag_ids = self._convert_tag_names_to_ids(asset.tags, self.tags)
        upstream_asset_tag_ids = self._get_tag_ids_from_tags(upstream_asset.tags)
        tag_ids += [
            tag_id for tag_id in upstream_asset_tag_ids if tag_id not in tag_ids
        ]
        tag_ids.sort()
        upstream_asset.tags = tag_ids
        if upstream_asset.updates():
            # There is a problem where netbox sometimes returns a 500 error
            # when you save an asset even though the asset is saved successfully.
            _logger.debug(f"Updating asset {upstream_asset.serial}")
            try:
                upstream_asset.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to update asset {upstream_asset.serial}: {str(e)}"
                )

    def _shelve_all_missing_assets(
        self,
        upstream_assets: dict[int, dict[SerialStr, Record]],
        new_assets: Sequence[Asset],
    ):
        """Shelve any upstream assets that were registered by navsync but were not found in the current sync"""
        new_assets_by_device_type_and_serial = {}
        for new_asset in new_assets:
            device_type = self.get_or_create_device_type(
                new_asset.manufacturer, new_asset.model
            )
            if device_type.id not in new_assets_by_device_type_and_serial:
                new_assets_by_device_type_and_serial[device_type.id] = {}
            new_assets_by_device_type_and_serial[device_type.id][new_asset.serial] = (
                new_asset
            )

        for device_type_id, assets in upstream_assets.items():
            for serial, upstream_asset in assets.items():
                if new_assets_by_device_type_and_serial.get(device_type_id, {}).get(
                    serial
                ):
                    continue

                tags = [tag.name for tag in upstream_asset.tags]
                if "navsync" not in tags or "cnaas" not in tags:
                    continue
                if upstream_asset.status != "used":
                    continue
                self._shelve_asset(upstream_asset)

    def _shelve_asset(self, upstream_asset: Record):
        """Shelve an asset by setting its status to 'stored' and unassigning it from any device"""
        _logger.debug(f"Shelving asset {upstream_asset.serial}")
        upstream_asset.status = "stored"
        upstream_asset.device = None
        # There is a problem where netbox sometimes returns a 500 error
        # when you save an asset even though the asset is saved successfully.
        try:
            upstream_asset.save()
        except RequestError as e:
            _logger.error(f"Failed to shelve asset {upstream_asset.serial}: {str(e)}")

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

    def _sync_devices(
        self,
        devices: dict[NameStr, Device],
        flat_locations: dict[str, dict[NameStr, Record]],
    ):
        upstream_devices_by_name = self._get_upstream_devices()
        upstream_sites = self._get_upstream_sites()
        upstream_device_roles_by_name = self._get_upstream_device_roles()

        self._decommission_all_missing_devices(
            devices, upstream_devices_by_name.values()
        )

        for device in devices.values():
            upstream_device = upstream_devices_by_name.get(device.name)
            upstream_site = self._get_upstream_site(
                upstream_sites, device.location.site
            )
            if not upstream_site:
                raise ValueError(
                    f"Could not find site {device.location.site.name}. It should have been created during `_sync_sites_and_locations`"
                )
            upstream_location = flat_locations.get(device.nav_server, {}).get(
                device.location.name
            )
            upstream_device_role = upstream_device_roles_by_name.get(device.role)
            if not upstream_device_role:
                raise ValueError(f"Could not find device_role {device.role}")

            if upstream_device:
                self._update_device(
                    device,
                    upstream_device,
                    upstream_site,
                    upstream_device_role,
                    upstream_location,
                )
            else:
                self._create_device(
                    device,
                    upstream_site,
                    upstream_device_role,
                    upstream_location,
                )

    def _decommission_all_missing_devices(
        self, devices: dict[NameStr, Device], upstream_devices: Sequence[Record]
    ):
        """Shelve any upstream devices that were registered by navsync but were not found in the current sync"""
        for upstream_device in upstream_devices:
            if upstream_device.name in devices:
                continue
            tags = [tag.name for tag in upstream_device.tags]
            if "navsync" not in tags or "cnaas" not in tags:
                continue
            if upstream_device.role and upstream_device.role.slug == "verktykassecnaas":
                continue
            if (
                upstream_device.status.value != "active"
                and upstream_device.status.value != "offline"
            ):
                continue
            _logger.debug(f"Decommissioning device {upstream_device.name}")
            upstream_device.status = "inventory"
            upstream_device.location = None
            upstream_device.tenant = None
            upstream_device.virtual_chassis = None
            upstream_device.vc_position = None
            try:
                upstream_device.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to decommission device {upstream_device.name}: {e}"
                )

    def _create_device(
        self,
        device: Device,
        upstream_site: Record,
        upstream_device_role: Record,
        upstream_location: Optional[Record] = None,
    ):
        try:
            upstream_device_type = self.get_or_create_device_type(
                device.manufacturer, device.model
            )
        except (RequestError, ValueError) as e:
            _logger.error(
                f"Got error while getting or creating device type for model {device.model}: {str(e)}. Skipping device {device.name}"
            )
            return

        tag_ids = self._convert_tag_names_to_ids(device.tags + ["cnaas"], self.tags)
        new_device_dict = {
            "name": device.name,
            "device_type": upstream_device_type.id,
            "role": upstream_device_role.id,
            "site": upstream_site.id,
            "tags": tag_ids,
            "tenant": device.tenant,
            "status": device.status,
        }
        if upstream_location:
            new_device_dict["location"] = upstream_location.id
        if device.url:
            new_device_dict["custom_fields"] = {"nav_url": device.url}

        _logger.debug(f"Creating new device {device.name}")
        try:
            self.netbox_api.dcim.devices.create(**new_device_dict)
        except RequestError as e:
            _logger.error(f"Failed to create device {device.name}: {str(e)}")

    def _update_device(
        self,
        device: Device,
        upstream_device: Record,
        upstream_site: Record,
        upstream_device_role: Record,
        upstream_location: Optional[Record] = None,
    ):
        if "cnaas" in [upstream_tag.name for upstream_tag in upstream_device.tags]:
            upstream_device.site = upstream_site.id
            if upstream_location:
                upstream_device.location = upstream_location.id

        tag_ids = self._convert_tag_names_to_ids(device.tags, self.tags)
        upstream_device_tag_ids = self._get_tag_ids_from_tags(upstream_device.tags)
        tag_ids += [
            tag_id for tag_id in upstream_device_tag_ids if tag_id not in tag_ids
        ]
        tag_ids.sort()
        upstream_device.tags = tag_ids

        upstream_device.role = upstream_device_role.id
        upstream_device.status = device.status
        upstream_device.tenant = device.tenant
        if device.url:
            upstream_device.custom_fields["nav_url"] = device.url
        try:
            upstream_device_type = self.get_or_create_device_type(
                device.manufacturer, device.model
            )
        except (RequestError, ValueError) as e:
            _logger.error(
                f"Got error while getting or creating device type for model {device.model}: {str(e)}. Not updating device type for device {device.name}."
            )
        else:
            upstream_device.device_type = upstream_device_type.id

        if upstream_device.updates():
            _logger.debug(f"Updating device {upstream_device.name}")
            try:
                upstream_device.save()
            except RequestError as e:
                _logger.error(f"Failed to update device {device.name}: {str(e)}")

    def _get_upstream_manufacturers(self) -> dict[SlugStr, Record]:
        """Maps slug to manufacturer Record"""
        upstream_manufacturers = self.netbox_api.dcim.manufacturers.all()
        return {m.slug: m for m in upstream_manufacturers}

    def get_or_create_device_type(self, manufacturer: str, model: str) -> Record:
        upstream_device_type = self._get_device_type(manufacturer, model)
        if not upstream_device_type:
            upstream_manufacturer = self.get_or_create_manifacturer(manufacturer)
            upstream_device_type = self.netbox_api.dcim.device_types.create(
                manufacturer=upstream_manufacturer.id,
                model=model,
                part_number=model,
                slug=sanitize_slug(f"{manufacturer}-{model}"),
            )

            # Update local dicts so it stays synced with netbox without needing to fetch again
            if upstream_manufacturer.slug not in self.upstream_device_types_by_model:
                self.upstream_device_types_by_model[upstream_manufacturer.slug] = {}
            self.upstream_device_types_by_model[upstream_manufacturer.slug][
                upstream_device_type.model
            ] = upstream_device_type
            if (
                upstream_manufacturer.slug
                not in self.upstream_device_types_by_part_number
            ):
                self.upstream_device_types_by_part_number[
                    upstream_manufacturer.slug
                ] = {}
            self.upstream_device_types_by_part_number[upstream_manufacturer.slug][
                upstream_device_type.part_number
            ] = upstream_device_type

        return upstream_device_type

    def get_or_create_manifacturer(self, name: str) -> Record:
        manufacturer_slug = sanitize_slug(name)
        upstream_manufacturer = self.upstream_manufacturers.get(manufacturer_slug)
        if not upstream_manufacturer:
            upstream_manufacturer = self.netbox_api.dcim.manufacturers.create(
                name=name,
                slug=manufacturer_slug,
            )
            self.upstream_manufacturers[upstream_manufacturer.slug] = (
                upstream_manufacturer
            )
        return upstream_manufacturer

    def _sync_sites_and_locations(
        self, sites: Sequence[Site]
    ) -> dict[str, dict[NameStr, Record]]:
        """Syncs sites and their nested location hierarchies to Netbox.
        Returns a map of nav_server -> location_name -> upstream Record for use by device syncing."""
        upstream_sites = self._get_upstream_sites()
        upstream_locations_by_slug, upstream_locations_by_name = (
            self._get_upstream_locations()
        )
        flat_locations: dict[str, dict[NameStr, Record]] = {}

        for site in sites:
            upstream_site = self._get_upstream_site(upstream_sites, site)
            if upstream_site:
                self._update_site(site, upstream_site)
            else:
                upstream_site = self._create_site(site)
                if upstream_site is None:
                    _logger.error(
                        f"Could not create site {site.name}; skipping location sync."
                    )
                    continue
                upstream_sites.append(upstream_site)

            self._sync_location_tree(
                site.locations.values(),
                upstream_site,
                upstream_locations_by_slug,
                upstream_locations_by_name,
                flat_locations,
            )

        return flat_locations

    def _sync_location_tree(
        self,
        locations: Sequence[Location],
        upstream_site: Record,
        upstream_locations_by_slug: dict[
            SlugStr, dict[SlugStr, dict[Optional[int], Record]]
        ],
        upstream_locations_by_name: dict[
            SlugStr, dict[NameStr, dict[Optional[int], Record]]
        ],
        flat_locations: dict[str, dict[NameStr, Record]],
        parent_id: Optional[int] = None,
    ):
        """Recursively syncs a location tree for a site, creating parents before children."""
        for location in locations:
            upstream_location = (
                upstream_locations_by_name.get(upstream_site.slug, {})
                .get(location.name, {})
                .get(parent_id)
            ) or (
                upstream_locations_by_slug.get(upstream_site.slug, {})
                .get(location.slug, {})
                .get(parent_id)
            )
            if upstream_location:
                self._update_location(
                    location,
                    upstream_location,
                    parent_id,
                )
            else:
                upstream_location = self._create_location(
                    location,
                    upstream_site,
                    parent_id,
                )
                if upstream_location is None:
                    continue
                upstream_locations_by_slug.setdefault(
                    upstream_site.slug, {}
                ).setdefault(location.slug, {})[parent_id] = upstream_location
                upstream_locations_by_name.setdefault(
                    upstream_site.slug, {}
                ).setdefault(location.name, {})[parent_id] = upstream_location

            flat_locations.setdefault(location.nav_server, {})[location.name] = (
                upstream_location
            )

            if location.child_locations:
                self._sync_location_tree(
                    location.child_locations.values(),
                    upstream_site,
                    upstream_locations_by_slug,
                    upstream_locations_by_name,
                    flat_locations,
                    upstream_location.id,
                )

    def _create_location(
        self,
        location: Location,
        upstream_site: Record,
        parent_id: Optional[int] = None,
    ) -> Optional[Record]:
        tag_ids = self._convert_tag_names_to_ids(location.tags + ["cnaas"], self.tags)
        new_location_dict = {
            "site": upstream_site.id,
            "name": location.name,
            "slug": location.slug,
            "status": location.status,
            "tags": tag_ids,
            "tenant": location.tenant,
        }
        if parent_id is not None:
            new_location_dict["parent"] = parent_id
        if location.description:
            new_location_dict["description"] = location.description
        if location.url:
            new_location_dict["custom_fields"] = {"nav_url": location.url}
        _logger.debug(
            f"Creating new location {location.name} under site {upstream_site.name}"
        )
        try:
            return self.netbox_api.dcim.locations.create(**new_location_dict)
        except RequestError as e:
            _logger.error(f"Failed to create location {location.name}: {e}")
            return None

    def _update_location(
        self,
        location: Location,
        upstream_location: Record,
        parent_id: Optional[int] = None,
    ):
        upstream_location.tenant = location.tenant
        if not upstream_location.description and location.description:
            upstream_location.description = location.description

        upstream_location.parent = parent_id

        tag_ids = self._convert_tag_names_to_ids(location.tags, self.tags)
        upstream_location_tag_ids = self._get_tag_ids_from_tags(upstream_location.tags)
        tag_ids += [
            tag_id for tag_id in upstream_location_tag_ids if tag_id not in tag_ids
        ]
        tag_ids.sort()

        upstream_location.tags = tag_ids
        upstream_location.status = location.status
        if location.url is not None:
            upstream_location.custom_fields["nav_url"] = location.url

        if upstream_location.updates():
            _logger.debug(f"Updating location {upstream_location.name}")
            try:
                upstream_location.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to update location {upstream_location.name}: {str(e)}"
                )

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
        if site.url is not None:
            upstream_site.custom_fields["nav_url"] = site.url

        tag_ids = self._convert_tag_names_to_ids(site.tags, self.tags)
        upstream_site_tag_ids = self._get_tag_ids_from_tags(upstream_site.tags)
        tag_ids += [tag_id for tag_id in upstream_site_tag_ids if tag_id not in tag_ids]
        tag_ids.sort()

        upstream_site.tags = tag_ids
        upstream_site.status = site.status
        if upstream_site.updates():
            _logger.debug(f"Updating site {upstream_site.name}")
            try:
                upstream_site.save()
            except RequestError as e:
                _logger.error(f"Failed to update site {upstream_site.name}: {str(e)}")

    def _create_site(self, site: Site) -> Optional[Record]:
        tag_ids = self._convert_tag_names_to_ids(site.tags + ["cnaas"], self.tags)
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
        if site.url:
            new_site_dict["custom_fields"] = {"nav_url": site.url}
        _logger.debug(f"Creating new site {site.name}")
        try:
            return self.netbox_api.dcim.sites.create(**new_site_dict)
        except RequestError as e:
            _logger.error(f"Failed to create site {site.name}: {e}")
            return None

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

    def _get_upstream_assets(self) -> dict[int, dict[SerialStr, Record]]:
        """Returns dict mapping device type IDs and serial numbers to assets. The first dict maps device type id to a dict of serial numbers, and the second dict maps serial numbers to asset Records"""
        assets = {}
        for asset in self.netbox_api.plugins.inventory.assets.all():
            if not asset.device_type or not asset.serial:
                continue
            if asset.device_type.id not in assets:
                assets[asset.device_type.id] = {}
            assets[asset.device_type.id][asset.serial] = asset
        return assets

    def _get_upstream_tenants(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to tenant"""
        return {tenant.name: tenant for tenant in self.netbox_api.tenancy.tenants.all()}

    def _get_upstream_device_types_by_part_number(
        self,
    ) -> dict[SlugStr, dict[str, Record]]:
        """Returns dict mapping manufacturer slug to part numbers and device types"""
        device_types = {}
        for device_type in self.netbox_api.dcim.device_types.all():
            if device_type.manufacturer.slug not in device_types:
                device_types[device_type.manufacturer.slug] = {}
            device_types[device_type.manufacturer.slug][device_type.part_number] = (
                device_type
            )
        return device_types

    def _get_upstream_device_types_by_model(self) -> dict[SlugStr, dict[str, Record]]:
        """Returns dict mapping manufacturer slug to models and device types"""
        device_types = {}
        for device_type in self.netbox_api.dcim.device_types.all():
            if device_type.manufacturer.slug not in device_types:
                device_types[device_type.manufacturer.slug] = {}
            device_types[device_type.manufacturer.slug][device_type.model] = device_type
        return device_types

    def _get_upstream_device_roles(self) -> dict[NameStr, Record]:
        """Returns dict mapping name to device role"""
        return {
            device_role.name: device_role
            for device_role in self.netbox_api.dcim.device_roles.all()
        }

    def _get_upstream_locations(
        self,
    ) -> tuple[
        dict[SlugStr, dict[SlugStr, dict[Optional[int], Record]]],
        dict[SlugStr, dict[NameStr, dict[Optional[int], Record]]],
    ]:
        """Returns (by_slug, by_name): site slug -> location slug/name -> parent id -> Record."""
        by_slug: dict[SlugStr, dict[SlugStr, dict[Optional[int], Record]]] = {}
        by_name: dict[SlugStr, dict[NameStr, dict[Optional[int], Record]]] = {}
        for location in self.netbox_api.dcim.locations.all():
            parent_id = location.parent.id if location.parent else None
            by_slug.setdefault(location.site.slug, {}).setdefault(location.slug, {})[
                parent_id
            ] = location
            by_name.setdefault(location.site.slug, {}).setdefault(location.name, {})[
                parent_id
            ] = location
        return by_slug, by_name

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
            if upstream_site.slug == site.slug or upstream_site.name == site.name:
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

    def _fetch_nav_data(self) -> NavData:
        """Fetches all data from all NAV servers in a single pass."""
        result = NavData()
        sites: dict[str, Site] = {}  # keyed by name for duplicate detection
        all_entities: list[VirtualChassis | Device] = []

        for nav_server in self._get_nav_servers():
            token = self._generate_nav_token(aud=nav_server.url)
            # Sleep to avoid issues with the `nbf` claim.
            time.sleep(1)
            server_sites, server_locations = LocationHierarchyParser(
                nav_server, token
            ).get_sites_and_locations()

            for site in server_sites.values():
                if site.name in sites:
                    raise ValueError(
                        f"Duplicate site name {site.name} found across NAV servers. Site names must be unique across all NAV servers."
                    )
                sites[site.name] = site
            result.locations.extend(server_locations.values())

            # Sleep to avoid issues with the `nbf` claim.
            time.sleep(1)
            all_entities.extend(
                EntityParser(
                    nav_server,
                    token,
                    locations=server_locations,
                    netbox_tenants={name: t.id for name, t in self.tenants.items()},
                ).parse()
            )

        result.chassis, result.devices = self._get_virtual_chassis_and_devices(
            all_entities
        )
        result.sites = list(sites.values())
        result.devices = self._handle_duplicate_serials(result.devices)
        return result

    def _get_virtual_chassis_and_devices(
        self,
        entities: list[VirtualChassis | Device],
    ) -> tuple[dict[NameStr, VirtualChassis], dict[NameStr, Device]]:
        virtual_chassises: dict[NameStr, VirtualChassis] = {}
        devices: dict[NameStr, Device] = {}
        for entity in entities:
            match entity:
                case VirtualChassis():
                    if entity.name in virtual_chassises:
                        _logger.error(
                            f"Duplicate virtual chassis name {entity.name}. Dropping duplicate."
                        )
                        continue
                    virtual_chassises[entity.name] = entity
                    for device in entity.devices:
                        if device.name in devices:
                            _logger.error(
                                f"Duplicate device name {device.name}. Dropping duplicate."
                            )
                            continue
                        devices[device.name] = device
                case Device():
                    if entity.name in devices:
                        _logger.error(
                            f"Duplicate physical chassis name {entity.name}. Dropping duplicate."
                        )
                        continue
                    devices[entity.name] = entity
                case _:
                    raise TypeError(f"Unexpected entity type {type(entity)}")
        return virtual_chassises, devices

    def _handle_duplicate_serials(self, devices: dict[NameStr, Device]):
        devices_copy = devices.copy()
        serials = self._group_devices_by_type_and_serial(devices.values())
        for manufacturer, serials_by_manufacturer in serials.items():
            for model, serials_by_model in serials_by_manufacturer.items():
                for serial, devices_with_serial in serials_by_model.items():
                    if len(devices_with_serial) > 1:
                        _logger.warning(
                            f"Duplicate asset {serial} found for manufacturer {manufacturer} and model {model} in devices {[device.name for device in devices_with_serial]}"
                        )
                    else:
                        continue
                    up_devices = [
                        device
                        for device in devices_with_serial
                        if not device.status == "offline"
                    ]
                    if len(up_devices) == 1:
                        device_to_keep = up_devices[0]
                        _logger.debug(
                            f"Device {device_to_keep.name} is up while the other device(s) with the same serial are down. Removing asset from the downed devices."
                        )
                        for device in devices_with_serial:
                            if device.status == "offline":
                                devices_copy[device.name].asset = None
                    elif len(up_devices) > 1:
                        _logger.error(
                            f"Multiple devices with asset {serial} are marked as up. Syncing devices without assets."
                        )
                        for device in devices_with_serial:
                            devices_copy[device.name].asset = None
                    else:
                        _logger.error(
                            f"All devices with asset {serial} are marked as down. Syncing devices without assets."
                        )
                        for device in devices_with_serial:
                            devices_copy[device.name].asset = None

        return devices_copy

    def _group_devices_by_type_and_serial(
        self,
        devices: Sequence[Device],
    ) -> dict[ManufacturerStr, dict[ModelStr, dict[SerialStr, list[Device]]]]:
        """Groups devices by manufacturer, model and serial number"""
        grouped_dict = {}
        for device in devices:
            if not device.asset:
                continue
            manufacturer = device.asset.manufacturer
            model = device.asset.model
            serial = device.asset.serial
            if manufacturer not in grouped_dict:
                grouped_dict[manufacturer] = {}
            if model not in grouped_dict[manufacturer]:
                grouped_dict[manufacturer][model] = {}
            if serial not in grouped_dict[manufacturer][model]:
                grouped_dict[manufacturer][model][serial] = []
            grouped_dict[manufacturer][model][serial].append(device)
        return grouped_dict

    def _generate_nav_token(self, aud: str):
        now = datetime.now(timezone.utc)
        jwt_claims = {
            "exp": (now + self.expiry_delta).timestamp(),
            "nbf": now.timestamp(),
            "iat": now.timestamp(),
            "aud": aud,
            "iss": self.issuer,
            "token_type": "access",
            "endpoints": [
                "/api/1/netbox",
                "/api/1/netboxentity",
                "/api/1/location",
                "/api/1/room",
                "/api/1/organization",
            ],
            "write": False,
        }
        return jwt.encode(jwt_claims, self.private_key, algorithm="RS256")

    def _get_nav_servers(self) -> Iterable[NavServerInfo]:
        """
        For each NAV server instance found on the Netbox server, yields a
        namespace containing that instance's url, owner, and tenant
        """
        virtual_machines = self.netbox_api.virtualization.virtual_machines.filter(
            role="verktykassecnaas", status="active"
        )
        devices = self.netbox_api.dcim.devices.filter(
            role="verktykassecnaas", status="active"
        )
        for vm in virtual_machines:
            if not vm.tags or "navsync" not in [tag.name for tag in vm.tags]:
                _logger.debug(
                    f"VM {vm.name} is missing tag 'navsync'. This means it should not be synced. Skipping."
                )
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
            if not device.tags or "navsync" not in [tag.name for tag in device.tags]:
                _logger.debug(
                    f"Device {device.name} is missing tag 'navsync'. This means it should not be synced. Skipping."
                )
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

    def _get_device_type(self, manufacturer: str, model: str) -> Optional[Record]:
        device_type = self._get_device_type_by_part_number(manufacturer, model)
        if device_type:
            return device_type
        return self._get_device_type_by_model(manufacturer, model)

    def _get_device_type_by_part_number(
        self, manufacturer: str, part_number: str
    ) -> Optional[Record]:
        return self.upstream_device_types_by_part_number.get(
            sanitize_slug(manufacturer), {}
        ).get(part_number)

    def _get_device_type_by_model(
        self, manufacturer: str, model: str
    ) -> Optional[Record]:
        return self.upstream_device_types_by_model.get(
            sanitize_slug(manufacturer), {}
        ).get(model)


if __name__ == "__main__":
    main()
