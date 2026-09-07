"""
Helper functions for reading objects out of a Netbox instance.

The fetch functions each retrieve a whole collection and return it as a map
keyed the way callers look those objects up, since fetching a collection once
and reusing the map is much cheaper than querying Netbox per object.

Every function takes the Netbox API as its first argument, so they can be used
without a syncer instance.
"""

from typing import Optional, Sequence

import pynetbox.core.api as netbox
from pynetbox.core.response import Record

NameStr = str
SlugStr = str
SerialStr = str


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
