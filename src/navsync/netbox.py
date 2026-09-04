"""
Helper functions for reading objects out of a Netbox instance.

The fetch functions each retrieve a whole collection and return it as a map
keyed the way callers look those objects up, since fetching a collection once
and reusing the map is much cheaper than querying Netbox per object.

Every function takes the Netbox API as its first argument, so they can be used
without a syncer instance.
"""

import logging
from ipaddress import ip_interface
from typing import Iterable, Optional, Sequence

import pynetbox.core.api as netbox
from pynetbox.core.response import Record

from navsync.utils import (
    NavServerInfo,
    url_with_http,
    url_with_https,
)

NameStr = str
SlugStr = str
SerialStr = str

# The Netbox role a device or VM must have to be treated as a NAV instance
VERKTOYKASSE_ROLE = "verktykassecnaas"

_logger = logging.getLogger(__name__)


def get_tenants(api: netbox.Api) -> dict[NameStr, Record]:
    """Returns dict mapping name to tenant"""
    return {tenant.name: tenant for tenant in api.tenancy.tenants.all()}


def get_manufacturers(api: netbox.Api) -> dict[SlugStr, Record]:
    """Returns dict mapping slug to manufacturer"""
    return {
        manufacturer.slug: manufacturer for manufacturer in api.dcim.manufacturers.all()
    }


def get_device_types_by_part_number(
    api: netbox.Api,
) -> dict[SlugStr, dict[str, Record]]:
    """Returns dict mapping manufacturer slug to part numbers and device types"""
    device_types: dict[SlugStr, dict[str, Record]] = {}
    for device_type in api.dcim.device_types.all():
        device_types.setdefault(device_type.manufacturer.slug, {})[
            device_type.part_number
        ] = device_type
    return device_types


def get_device_types_by_model(api: netbox.Api) -> dict[SlugStr, dict[str, Record]]:
    """Returns dict mapping manufacturer slug to models and device types"""
    device_types: dict[SlugStr, dict[str, Record]] = {}
    for device_type in api.dcim.device_types.all():
        device_types.setdefault(device_type.manufacturer.slug, {})[
            device_type.model
        ] = device_type
    return device_types


def get_device_roles(api: netbox.Api) -> dict[NameStr, Record]:
    """Returns dict mapping name to device role"""
    return {
        device_role.name: device_role for device_role in api.dcim.device_roles.all()
    }


def get_devices(api: netbox.Api) -> dict[NameStr, Record]:
    """Returns dict mapping name to device"""
    return {device.name: device for device in api.dcim.devices.all()}


def get_sites(api: netbox.Api) -> list[Record]:
    """Returns a list of all sites"""
    return list(api.dcim.sites.all())


def get_locations(
    api: netbox.Api,
) -> tuple[
    dict[SlugStr, dict[SlugStr, dict[Optional[int], Record]]],
    dict[SlugStr, dict[NameStr, dict[Optional[int], Record]]],
]:
    """
    Returns (by_slug, by_name): site slug -> location slug/name -> parent id ->
    location.

    Locations are keyed by their parent as well as their name, since the same
    location name may occur more than once in a site as long as the duplicates
    sit under different parents.
    """
    by_slug: dict[SlugStr, dict[SlugStr, dict[Optional[int], Record]]] = {}
    by_name: dict[SlugStr, dict[NameStr, dict[Optional[int], Record]]] = {}
    for location in api.dcim.locations.all():
        parent_id = location.parent.id if location.parent else None
        by_slug.setdefault(location.site.slug, {}).setdefault(location.slug, {})[
            parent_id
        ] = location
        by_name.setdefault(location.site.slug, {}).setdefault(location.name, {})[
            parent_id
        ] = location
    return by_slug, by_name


def get_virtual_chassis(api: netbox.Api) -> dict[NameStr, Record]:
    """Returns dict mapping name to virtual chassis"""
    return {chassis.name: chassis for chassis in api.dcim.virtual_chassis.all()}


def get_assets(api: netbox.Api) -> dict[int, dict[SerialStr, Record]]:
    """
    Returns dict mapping device type id to serial numbers and assets.

    Assets without a device type or without a serial number are left out, since
    neither can be looked up.
    """
    assets: dict[int, dict[SerialStr, Record]] = {}
    for asset in api.plugins.inventory.assets.all():
        if not asset.device_type or not asset.serial:
            continue
        assets.setdefault(asset.device_type.id, {})[asset.serial] = asset
    return assets


def get_interfaces(api: netbox.Api) -> dict[int, dict[NameStr, Record]]:
    """
    Returns dict mapping device id to interface names and interfaces.

    Interface names are only unique within a device, so the device id is part of
    the key. Interfaces that are not attached to a device are left out.
    """
    interfaces: dict[int, dict[NameStr, Record]] = {}
    for interface in api.dcim.interfaces.all():
        if not interface.device:
            continue
        interfaces.setdefault(interface.device.id, {})[interface.name] = interface
    return interfaces


def get_ip_addresses(api: netbox.Api) -> dict[str, Record]:
    """
    Returns dict mapping host address (without mask, e.g. '10.0.0.1') to IP
    address.

    Netbox enforces uniqueness on the host address, not on the address together
    with its mask, so '10.0.0.1/24' and '10.0.0.1/32' are the same address to
    Netbox. Keying on the host address means an address whose mask has changed
    in NAV is recognized, instead of being created again and rejected as a
    duplicate.

    Addresses that cannot be parsed are left out.
    """
    ip_addresses: dict[str, Record] = {}
    for ip_address in api.ipam.ip_addresses.all():
        host = host_address(str(ip_address.address))
        if host is None:
            continue
        ip_addresses[host] = ip_address
    return ip_addresses


def host_address(address: str) -> Optional[str]:
    """
    Returns the host part of an address in CIDR notation, e.g. '10.0.0.1' for
    '10.0.0.1/24'. Returns None if the address cannot be parsed.
    """
    try:
        return str(ip_interface(address).ip)
    except ValueError:
        _logger.warning("Could not parse IP address %r", address)
        return None


def get_nav_servers(api: netbox.Api, https: bool) -> Iterable[NavServerInfo]:
    """
    For each NAV server instance found on the Netbox server, yields a namespace
    containing that instance's url, owner, and tenant.

    A NAV instance is a device or VM with the 'verktøykasse' role, and its name
    is the host to reach it on.

    :param https: whether the NAV instances are reachable over https
    """
    virtual_machines = api.virtualization.virtual_machines.filter(
        role=VERKTOYKASSE_ROLE, status="active"
    )
    devices = api.dcim.devices.filter(role=VERKTOYKASSE_ROLE, status="active")
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
            url=_url_from_name(vm.name, https),
            owner_id=vm.custom_fields["owner"]["id"],
            tenant_id=vm.tenant.id,
        )

    for device in devices:
        if not device.tags or "navsync" not in [tag.name for tag in device.tags]:
            _logger.debug(
                f"Device {device.name} is missing tag 'navsync'. This means it should not be synced. Skipping."
            )
            continue
        asset = api.plugins.inventory.assets.get(device=device)
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
            url=_url_from_name(device.name, https),
            owner_id=asset.owner.id,
            tenant_id=device.tenant.id,
        )


def _url_from_name(device_name: str, https: bool) -> str:
    if https:
        return url_with_https(device_name)
    else:
        return url_with_http(device_name)


# The functions below search collections that have already been fetched, rather
# than reading from Netbox, for objects that cannot be keyed usefully in a map.


def find_site(
    sites: Sequence[Record], name: NameStr, slug: SlugStr
) -> Optional[Record]:
    """
    Returns the site matching either the given slug or the given name, or None
    if there is no match.

    A site is looked up by both, since a site that was renamed upstream still
    matches on its slug, and one whose slug was changed still matches on its
    name.
    """
    for site in sites:
        if site.slug == slug or site.name == name:
            return site
    return None


def find_asset_for_device(assets: Sequence[Record], device_id: int) -> Optional[Record]:
    """
    Returns the asset currently assigned to the given device, or None if no
    asset is assigned to it.
    """
    for asset in assets:
        if asset.device is None:
            continue
        # 'device' is a Record when it comes straight from Netbox, but an int if
        # it has been modified locally
        if isinstance(asset.device, int):
            asset_device_id = asset.device
        else:
            asset_device_id = asset.device.id
        if asset_device_id == device_id:
            return asset
    return None


def find_device_in_vc_position(
    devices: Sequence[Record], virtual_chassis_id: int, position: int
) -> Optional[Record]:
    """
    Returns the device registered in the given position of the given virtual
    chassis, or None if that position is unoccupied.
    """
    for device in devices:
        if not device.virtual_chassis:
            continue
        # 'virtual_chassis' is a Record when it comes straight from Netbox, but
        # an int if it has been modified locally
        if isinstance(device.virtual_chassis, int):
            device_virtual_chassis_id = device.virtual_chassis
        else:
            device_virtual_chassis_id = device.virtual_chassis.id
        if (
            device_virtual_chassis_id == virtual_chassis_id
            and device.vc_position == position
        ):
            return device
    return None
