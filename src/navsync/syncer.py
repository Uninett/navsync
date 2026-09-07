import logging
from datetime import timedelta
from ipaddress import ip_address
from typing import Optional, Sequence, Union

import pynetbox.core.api as netbox
from dynaconf import Dynaconf
from pynetbox.core.query import RequestError
from pynetbox.core.response import Record

from navsync import netbox as netbox_helpers
from navsync.parser import (
    Asset,
    Device,
    Interface,
    IpAddress,
    Location,
    NameStr,
    NavFetcher,
    Prefix,
    SerialStr,
    Site,
    SlugStr,
    VirtualChassis,
)
from navsync.utils import sanitize_slug

_logger = logging.getLogger(__name__)


class Syncer:
    """
    Performs all the logic needed to sync models from NAV to Netbox.

    Should be instantiated using :meth from_settings:
    Use :meth sync: to sync
    """

    netbox_api: netbox.Api
    private_key: str
    expiry_delta: timedelta
    issuer: str
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
        self.tenants = netbox_helpers.get_tenants(self.netbox_api)
        nav_fetcher = NavFetcher(
            private_key=self.private_key,
            expiry_delta=self.expiry_delta,
            issuer=self.issuer,
            netbox_tenants={name: t.id for name, t in self.tenants.items()},
        )
        nav_data = nav_fetcher.fetch(
            nav_servers=netbox_helpers.get_nav_servers(self.netbox_api, self.https)
        )
        assets = {
            device.name: device.asset
            for device in nav_data.devices.values()
            if device.asset is not None
        }

        if self.nosync:
            _logger.info("Nosync flag is set, not syncing to Netbox")
            return

        interfaces = [
            interface
            for device in nav_data.devices.values()
            for interface in device.interfaces
        ]
        ip_addresses = [
            address for interface in interfaces for address in interface.addresses
        ]
        prefixes = list(self._get_prefixes_to_sync(nav_data.devices).values())
        self.tags = self._get_or_create_tags(
            nav_data.sites
            + nav_data.locations
            + list(nav_data.devices.values())
            + list(assets.values())
            + interfaces
            + ip_addresses
            + prefixes
        )
        self.upstream_device_types_by_part_number = (
            netbox_helpers.get_device_types_by_part_number(self.netbox_api)
        )
        self.upstream_device_types_by_model = netbox_helpers.get_device_types_by_model(
            self.netbox_api
        )
        self.upstream_manufacturers = netbox_helpers.get_manufacturers(self.netbox_api)

        flat_locations = self._sync_sites_and_locations(nav_data.sites)
        self._sync_devices(nav_data.devices, flat_locations)
        self._sync_assets(assets)
        self._sync_prefixes(nav_data.devices)
        self._sync_interfaces_and_ip_addresses(nav_data.devices)

        self._sync_virtual_chassis(nav_data.chassis)

    @classmethod
    def from_settings(cls, settings: Dynaconf, nosync: bool = False):
        """
        Initialize a syncer based on user-supplied settings

        :param settings: the netbox-tools config-file, already parsed and validated
        :param nosync: only get data from the NAV instances, without making any
            changes in Netbox
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
            nosync=nosync,
        )

    def _sync_virtual_chassis(self, chassis: dict[NameStr, VirtualChassis]):
        upstream_chassis = netbox_helpers.get_virtual_chassis(self.netbox_api)
        upstream_devices = netbox_helpers.get_devices(self.netbox_api)

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
            _logger.debug(
                f"Updating virtual chassis {upstream_virtual_chassis.name}: {upstream_virtual_chassis.updates()}"
            )
            try:
                upstream_virtual_chassis.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to update virtual chassis {upstream_virtual_chassis.name}: {e}"
                )

    def _sync_assets(self, assets: dict[NameStr, Asset]):
        upstream_devices_by_name = netbox_helpers.get_devices(self.netbox_api)
        upstream_assets = netbox_helpers.get_assets(self.netbox_api)
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

            prior_asset = netbox_helpers.find_asset_for_device(
                upstream_asset_list, upstream_device.id
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
            _logger.debug(
                f"Updating asset {upstream_asset.serial}: {upstream_asset.updates()}"
            )
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

    def _sync_devices(
        self,
        devices: dict[NameStr, Device],
        flat_locations: dict[str, dict[NameStr, Record]],
    ):
        upstream_devices_by_name = netbox_helpers.get_devices(self.netbox_api)
        upstream_sites = netbox_helpers.get_sites(self.netbox_api)
        upstream_device_roles_by_name = netbox_helpers.get_device_roles(self.netbox_api)

        self._decommission_all_missing_devices(
            devices, upstream_devices_by_name.values()
        )

        for device in devices.values():
            upstream_device = upstream_devices_by_name.get(device.name)
            upstream_site = netbox_helpers.find_site(
                upstream_sites,
                name=device.location.site.name,
                slug=device.location.site.slug,
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

    def _sync_prefixes(self, devices: dict[NameStr, Device]):
        """
        Registers every prefix NAV reports for the devices' IP addresses, so
        that the addresses end up nested under a parent prefix in Netbox.

        Netbox nests an IP address under a prefix purely by containment, so
        there is nothing to link up afterwards: the prefix only has to exist.
        The same prefix is typically reported by many interfaces on many
        devices, so they are deduplicated before anything is sent to Netbox.
        """
        prefixes = self._get_prefixes_to_sync(devices)
        if not prefixes:
            _logger.debug("No prefixes to sync")
            return

        upstream_prefixes = netbox_helpers.get_prefixes(self.netbox_api)
        for prefix in prefixes.values():
            self._sync_prefix(prefix, upstream_prefixes)

    @staticmethod
    def _get_prefixes_to_sync(devices: dict[NameStr, Device]) -> dict[str, Prefix]:
        """
        Returns every distinct prefix reported for the devices' IP addresses,
        keyed by the prefix itself
        """
        return {
            address.prefix.prefix: address.prefix
            for device in devices.values()
            for interface in device.interfaces
            for address in interface.addresses
            if address.prefix is not None
        }

    def _sync_prefix(
        self, prefix: Prefix, upstream_prefixes: dict[str, Record]
    ) -> Optional[Record]:
        """
        Makes sure the given prefix exists in Netbox, so that the IP addresses
        inside it get a parent prefix. Returns the prefix, or None if it could
        neither be created nor found.

        An existing prefix is left untouched apart from its tags: it may have
        been created and curated by hand, and NAV has nothing to contribute to
        it beyond its existence.

        Any created prefix is added to 'upstream_prefixes' so that it is found,
        rather than created again, later in the same sync.
        """
        upstream_prefix = upstream_prefixes.get(prefix.prefix)
        if upstream_prefix is not None:
            self._update_prefix(prefix, upstream_prefix)
            return upstream_prefix

        tag_ids = self._convert_tag_names_to_ids(prefix.tags + ["cnaas"], self.tags)
        new_prefix_dict = {
            "prefix": prefix.prefix,
            "status": "active",
            "tags": tag_ids,
        }

        _logger.debug(f"Creating new prefix {prefix.prefix}")
        try:
            created_prefix = self.netbox_api.ipam.prefixes.create(**new_prefix_dict)
        except RequestError as e:
            _logger.error(f"Failed to create prefix {prefix.prefix}: {str(e)}")
            return None
        upstream_prefixes[prefix.prefix] = created_prefix
        return created_prefix

    def _update_prefix(self, prefix: Prefix, upstream_prefix: Record):
        """
        Marks an existing Netbox prefix as being seen by navsync, without
        changing anything else about it.
        """
        tag_ids = self._convert_tag_names_to_ids(prefix.tags, self.tags)
        upstream_prefix_tag_ids = self._get_tag_ids_from_tags(upstream_prefix.tags)
        tag_ids += [
            tag_id for tag_id in upstream_prefix_tag_ids if tag_id not in tag_ids
        ]
        tag_ids.sort()
        upstream_prefix.tags = tag_ids

        if upstream_prefix.updates():
            _logger.debug(
                f"Updating prefix {upstream_prefix.prefix}: {upstream_prefix.updates()}"
            )
            try:
                upstream_prefix.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to update prefix {upstream_prefix.prefix}: {str(e)}"
                )

    def _sync_interfaces_and_ip_addresses(self, devices: dict[NameStr, Device]):
        """
        Syncs every interface that NAV reports an IP address for, along with all
        those IP addresses, and registers the device's management IP address as
        its primary IP address.

        Netbox requires an IP address to be assigned to an interface on a
        device before it can be used as that device's primary IP address, so
        the interfaces are created (or updated) first.
        """
        devices_with_interfaces = {
            name: device for name, device in devices.items() if device.interfaces
        }
        if not devices_with_interfaces:
            _logger.debug("No devices with interfaces to sync")
            return

        upstream_devices_by_name = netbox_helpers.get_devices(self.netbox_api)
        upstream_interfaces = netbox_helpers.get_interfaces(self.netbox_api)
        upstream_ip_addresses = netbox_helpers.get_ip_addresses(self.netbox_api)

        for device_name, device in devices_with_interfaces.items():
            upstream_device = upstream_devices_by_name.get(device_name)
            if upstream_device is None:
                _logger.error(
                    f"Could not find device {device_name} when syncing its "
                    f"interfaces. Skipping its interfaces and IP addresses."
                )
                continue

            for interface in device.interfaces:
                upstream_interface = self._sync_interface(
                    interface, upstream_device, upstream_interfaces
                )
                if upstream_interface is None:
                    continue

                for address in interface.addresses:
                    upstream_ip_address = self._sync_ip_address(
                        address, upstream_interface, upstream_ip_addresses
                    )
                    if upstream_ip_address is None:
                        continue

                    if address.is_primary:
                        self._set_primary_ip_address(
                            upstream_device, upstream_ip_address
                        )

    def _sync_interface(
        self,
        interface: Interface,
        upstream_device: Record,
        upstream_interfaces: dict[int, dict[NameStr, Record]],
    ) -> Optional[Record]:
        """
        Makes sure the Netbox interface with the given name exists on the given
        device and matches the data from NAV, creating it if it does not already
        exist and updating it if it does. Returns the interface, or None if it
        could neither be created nor found.

        Any created interface is added to 'upstream_interfaces' so that it is
        found, rather than created again, later in the same sync.
        """
        upstream_interface = upstream_interfaces.get(upstream_device.id, {}).get(
            interface.name
        )
        if upstream_interface:
            self._update_interface(interface, upstream_interface)
            return upstream_interface

        tag_ids = self._convert_tag_names_to_ids(interface.tags + ["cnaas"], self.tags)
        new_interface_dict = {
            "device": upstream_device.id,
            "name": interface.name,
            "type": interface.type,
            "enabled": interface.enabled,
            "tags": tag_ids,
        }
        if interface.description:
            new_interface_dict["description"] = interface.description

        _logger.debug(
            f"Creating new interface {interface.name} on device {upstream_device.name}"
        )
        try:
            created_interface = self.netbox_api.dcim.interfaces.create(
                **new_interface_dict
            )
        except RequestError as e:
            _logger.error(
                f"Failed to create interface {interface.name} on device "
                f"{upstream_device.name}: {str(e)}"
            )
            return None
        upstream_interfaces.setdefault(upstream_device.id, {})[interface.name] = (
            created_interface
        )
        return created_interface

    def _update_interface(self, interface: Interface, upstream_interface: Record):
        """Updates an existing Netbox interface to match the data from NAV"""
        if interface.description:
            upstream_interface.description = interface.description

        tag_ids = self._convert_tag_names_to_ids(interface.tags, self.tags)
        upstream_interface_tag_ids = self._get_tag_ids_from_tags(
            upstream_interface.tags
        )
        tag_ids += [
            tag_id for tag_id in upstream_interface_tag_ids if tag_id not in tag_ids
        ]
        tag_ids.sort()
        upstream_interface.tags = tag_ids

        if upstream_interface.updates():
            _logger.debug(
                f"Updating interface {upstream_interface.name} on device "
                f"{upstream_interface.device.name}: {upstream_interface.updates()}"
            )
            try:
                upstream_interface.save()
            except RequestError as e:
                _logger.error(
                    f"Failed to update interface {upstream_interface.name} on device "
                    f"{upstream_interface.device.name}: {str(e)}"
                )

    def _sync_ip_address(
        self,
        address: IpAddress,
        upstream_interface: Record,
        upstream_ip_addresses: dict[str, Record],
    ) -> Optional[Record]:
        """
        Makes sure the given IP address exists in Netbox and is assigned to the
        given interface. Returns the IP address, or None if it could neither be
        found nor created.

        Any created IP address is added to 'upstream_ip_addresses' so that it is
        found, rather than created again, later in the same sync.

        'upstream_ip_addresses' is keyed by host address rather than by address
        and mask, since that is what Netbox enforces uniqueness on.
        """
        host = netbox_helpers.host_address(address.address)
        if host is None:
            _logger.error(
                f"Not syncing unparseable IP address {address.address} on "
                f"interface {upstream_interface.name}"
            )
            return None
        upstream_ip_address = upstream_ip_addresses.get(host)

        if upstream_ip_address is None:
            tag_ids = self._convert_tag_names_to_ids(
                address.tags + ["cnaas"], self.tags
            )
            new_ip_address_dict = {
                "address": address.address,
                "status": "active",
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": upstream_interface.id,
                "tags": tag_ids,
            }
            _logger.debug(
                f"Creating new IP address {address.address} on interface "
                f"{upstream_interface.name}"
            )
            try:
                created_ip_address = self.netbox_api.ipam.ip_addresses.create(
                    **new_ip_address_dict
                )
            except RequestError as e:
                _logger.error(
                    f"Failed to create IP address {address.address} on interface "
                    f"{upstream_interface.name}: {str(e)}"
                )
                return None
            upstream_ip_addresses[host] = created_ip_address
            return created_ip_address

        # Netbox treats '10.0.0.1/24' and '10.0.0.1/32' as the same address, so
        # an address already registered with another mask is found here rather
        # than being created again and rejected as a duplicate. Its mask is left
        # alone: it may have been set deliberately, and navsync has no better
        # information than whoever registered it.
        if str(upstream_ip_address.address) != address.address:
            _logger.debug(
                f"IP address {upstream_ip_address.address} is already registered "
                f"with a different mask than {address.address}. Keeping the "
                "existing mask."
            )

        # The IP address already exists in Netbox. Make sure it is assigned to
        # the interface NAV reports it on, since it may have moved between
        # interfaces or devices since the last sync.
        already_assigned = (
            upstream_ip_address.assigned_object_type == "dcim.interface"
            and upstream_ip_address.assigned_object_id == upstream_interface.id
        )
        if not already_assigned:
            # Netbox refuses to reassign an address that is designated as some
            # device's primary IP, so that designation has to go first
            self._release_primary_ip_address(upstream_ip_address)
            _logger.debug(
                f"Reassigning IP address {address.address} to interface "
                f"{upstream_interface.name} on device {upstream_interface.device.name}"
            )
            upstream_ip_address.assigned_object_type = "dcim.interface"
            upstream_ip_address.assigned_object_id = upstream_interface.id

        tag_ids = self._convert_tag_names_to_ids(address.tags, self.tags)
        upstream_tag_ids = self._get_tag_ids_from_tags(upstream_ip_address.tags)
        tag_ids += [tag_id for tag_id in upstream_tag_ids if tag_id not in tag_ids]
        tag_ids.sort()
        upstream_ip_address.tags = tag_ids

        if upstream_ip_address.updates():
            _logger.debug(
                f"Updating IP address {upstream_ip_address.address}: "
                f"{upstream_ip_address.updates()}"
            )
            try:
                upstream_ip_address.save()
                # Needs to be done to reset cached values for if fields have been modified
                upstream_ip_address.full_details()
            except RequestError as e:
                _logger.error(
                    f"Failed to update IP address {upstream_ip_address.address}: "
                    f"{str(e)}"
                )
                return None
        return upstream_ip_address

    @staticmethod
    def _primary_ip_field(upstream_ip_address: Record) -> Optional[str]:
        """
        Returns the name of the device field an IP address of this version can
        be the primary IP in, or None if the version cannot be determined.

        Netbox keeps the two address families in separate fields, and an
        address can only ever occupy the one matching its own version.
        """
        address = str(upstream_ip_address.address)
        try:
            version = ip_address(address.split("/")[0]).version
        except ValueError:
            _logger.error(f"Could not determine IP version of {address}")
            return None
        return "primary_ip4" if version == 4 else "primary_ip6"

    def _release_primary_ip_address(self, upstream_ip_address: Record):
        """
        Clears the given IP address from the primary IP address field of the
        device still designating it as such, if there is one.

        Netbox rejects reassigning an address to an interface on another device
        while that address is designated as the primary IP of its current
        device, so a device replaced in NAV would otherwise keep the address
        hostage and the reassignment would fail on every sync.

        The device is tagged 'navsync' as part of this, since navsync has now
        modified it, and it may not have been synced by navsync before.
        """
        field = self._primary_ip_field(upstream_ip_address)
        if field is None:
            return

        # The primary IP fields are one-to-one, so at most one device can
        # designate a given address as its primary IP
        holder = self.netbox_api.dcim.devices.get(
            **{f"{field}_id": upstream_ip_address.id}
        )
        if holder is None:
            return

        _logger.debug(
            f"Clearing {field} of device {holder.name} to free up IP "
            f"address {upstream_ip_address.address}"
        )
        setattr(holder, field, None)

        tag_ids = self._convert_tag_names_to_ids(["navsync"], self.tags)
        holder_tag_ids = self._get_tag_ids_from_tags(holder.tags)
        tag_ids += [tag_id for tag_id in holder_tag_ids if tag_id not in tag_ids]
        tag_ids.sort()
        holder.tags = tag_ids

        try:
            holder.save()
            # Needs to be done to reset cached values for if fields have been modified
            holder.full_details()
        except RequestError as e:
            _logger.error(f"Failed to clear {field} of device {holder.name}: {str(e)}")

    def _set_primary_ip_address(
        self, upstream_device: Record, upstream_ip_address: Record
    ):
        """
        Registers the given IP address as the primary IP address of the given
        device. The IP address must already be assigned to an interface on that
        device.
        """
        field = self._primary_ip_field(upstream_ip_address)
        if field is None:
            _logger.error(
                f"Not setting {upstream_ip_address.address} as primary IP address "
                f"of device {upstream_device.name}"
            )
            return
        current = getattr(upstream_device, field, None)
        if current and current.id == upstream_ip_address.id:
            # Already registered as the device's primary IP address
            return

        setattr(upstream_device, field, upstream_ip_address.id)
        _logger.debug(
            f"Setting {field} of device {upstream_device.name} to "
            f"{upstream_ip_address.address}"
        )
        try:
            upstream_device.save()
            # Needs to be done to reset cached values for if fields have been modified
            upstream_device.full_details()
        except RequestError as e:
            _logger.error(
                f"Failed to set {field} of device {upstream_device.name} to "
                f"{upstream_ip_address.address}: {str(e)}"
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
            _logger.debug(
                f"Updating device {upstream_device.name}: {upstream_device.updates()}"
            )
            try:
                upstream_device.save()
            except RequestError as e:
                _logger.error(f"Failed to update device {device.name}: {str(e)}")

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
        upstream_sites = netbox_helpers.get_sites(self.netbox_api)
        upstream_locations_by_slug, upstream_locations_by_name = (
            netbox_helpers.get_locations(self.netbox_api)
        )
        flat_locations: dict[str, dict[NameStr, Record]] = {}

        for site in sites:
            upstream_site = netbox_helpers.find_site(
                upstream_sites, name=site.name, slug=site.slug
            )
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
            _logger.debug(
                f"Updating location {upstream_location.name}: {upstream_location.updates()}"
            )
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
            _logger.debug(
                f"Updating site {upstream_site.name}: {upstream_site.updates()}"
            )
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
        self,
        objects: list[
            Union[
                Site,
                Location,
                Device,
                Asset,
                VirtualChassis,
                Interface,
                IpAddress,
                Prefix,
            ]
        ],
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

                prior_stack_member = netbox_helpers.find_device_in_vc_position(
                    upstream_devices.values(),
                    virtual_chassis.id,
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
