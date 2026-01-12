import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Literal, NewType, Self, Sequence

from navsync.nav import Api, NavBox, NavBoxEntity
from navsync.utils import NavServerInfo

_logger = logging.getLogger(__name__)

LowerStr = NewType("LowerStr", str)
ManufacturerStr = LowerStr
ModelStr = LowerStr
SlugStr = str
SerialStr = str
NameStr = str


@dataclass
class Site:
    """Information about a Netbox site instance"""

    name: str
    tags: list[str]
    slug: str
    latitude: float
    longitude: float
    tenant: int
    status: Literal["active", "decommissioning", "planned", "retired", "staging"] = (
        "active"
    )
    physical_address: str | None = None
    region: int | None = None
    description: str | None = None
    comments: str | None = None


@dataclass
class Location:
    """Information about a Netbox location instance"""

    name: str
    tags: list[str]
    tenant: int
    site: Site
    description: str | None = None
    comments: str | None = None
    parent: str | Self | None = None
    status: Literal["active", "decommissioning", "planned", "retired", "staging"] = (
        "active"
    )


@dataclass
class Asset:
    """Information about a Netbox asset instance"""

    tags: list[str]
    tenant: str
    owner: int
    status: Literal["stored", "used", "retired"] = "used"
    serial: SerialStr | None = None
    manufacturer: ManufacturerStr | None = None
    model: ModelStr | None = None
    comments: str | None = None
    contact: int | str | None = None
    navbox: NavBox | None = None


@dataclass
class Device:
    """Information about a Netbox device instance"""

    name: str
    tags: list[str]
    tenant: str
    asset: Asset
    location: Location
    role: Literal["router", "switch", "unknown", "PDU"]
    manufacturer: ManufacturerStr | None = None
    model: ModelStr | None = None
    vc_position: int | None = None
    description: str | None = None
    comments: str | None = None
    navbox: NavBox | None = None


PhysicalChassis = NewType("PhysicalChassis", Device)


@dataclass
class VirtualChassis:
    """Information about a Netbox virtual chassis instance"""

    name: str
    tags: list[str]
    tenant: int
    devices: list[Device]
    description: str | None = None
    comments: str | None = None
    navbox: NavBox | None = None


class IANAPhysicalClass(IntEnum):
    OTHER = 1
    UNKNOWN = 2
    CHASSIS = 3
    BACKPLANE = 4
    CONTAINER = 5  # e.g., chassis slot or daughter-card holder
    POWERSUPPLY = 6
    FAN = 7
    SENSOR = 8
    MODULE = 9  # e.g., plug-in card or daughter-card
    PORT = 10
    STACK = 11  # e.g., stack of multiple chassis entities
    CPU = 12
    ENERGYOBJECT = 13
    BATTERY = 14


def get_netbox_entities(
    nav_server_info: NavServerInfo, token: str
) -> list[PhysicalChassis | VirtualChassis]:
    """
    Gets data from all navboxes in a NAV server and parses them into equivalent
    Netbox entities.
    """
    nav_api = Api(url=nav_server_info.url, token=token)
    navboxes = nav_api.get_navboxes()

    # How a navbox should be parsed to Netbox varies based on the entities
    # it contains. Thus the overall parse logic is to try parse methods in
    # succession until one doesn't fail.
    #
    # Some parse methods 'depend' on other, more specific methods having
    # failed first, because they only read a subset of the entity data that
    # other methods read; therefore if allowed to run first they would
    # overshadow these more specific (and more correct) methods.  Thus we
    # define an order the parse methods should be attempted.
    schedule_attempt_order: list[
        Callable[[NavBox, NavServerInfo], PhysicalChassis | VirtualChassis]
    ] = [
        # Depends on nothing
        _try_parse_standard_virtual_chassis,
        # Depends on nothing
        _try_parse_juniper_virtual_chassis,
        # Depends on nothing
        _try_parse_standard_virtual_router,
        # Depends on:
        # - All other attempts except: _try_parse_physical_chassis
        _try_parse_unknown,
        # Depends on:
        # - All other attempts, this is regarded as a fallback
        _try_parse_physical_chassis,
    ]

    netbox_entities: list[PhysicalChassis | VirtualChassis] = []
    for navbox in navboxes:
        included_attempts = None
        for schedule_attempt in schedule_attempt_order:
            if (
                included_attempts is not None
                and schedule_attempt not in included_attempts
            ):
                continue
            try:
                netbox_entities.append(schedule_attempt(navbox, nav_server_info))
            except NextAttempt as err:
                if err.include is not None:
                    if included_attempts is None:
                        included_attempts = err.include
                    else:
                        included_attempts = [
                            attempt
                            for attempt in included_attempts
                            if attempt in err.include
                        ]
                # Continue with next parse attempt
                continue
            else:
                # Break the loop because no exceptions were raised, and thus we
                # assume this parse attempt was successful
                _logger.debug(f"Successfully parsed Navbox {navbox.sysname}")
                break
        else:
            _logger.warning(f"Skipped parsing Navbox {navbox.sysname}")
    return netbox_entities


def get_locations_from_devices(
    devices: Sequence[Device],
) -> list[Location]:
    """Extracts unique locations from a sequence of devices"""
    locations = []
    name_set = set()
    for device in devices:
        location = device.location
        # Only include one location per name
        if location.name not in name_set:
            locations.append(location)
            name_set.add(location.name)
    return locations


def get_sites_from_locations(
    locations: Sequence[Location],
) -> list[Site]:
    """Extracts unique sites from a sequence of locations"""
    sites = []
    slug_set = set()
    for location in locations:
        site = location.site
        # Only include one site per slug
        if site.slug not in slug_set:
            sites.append(site)
            slug_set.add(site.slug)
    return sites


def _try_parse_standard_virtual_chassis(
    navbox: NavBox, navinfo: NavServerInfo
) -> VirtualChassis:
    """
    Checks if navbox consists of:
    - *one* stack entity (that will represent the virtual chassis entity); this stack must have no parent
    - *one or more* chassis entities (that will represent physical chassis entities)

    If so, attempts to parse as virtual chassis
    Otherwise, skips ahead to either next parse attempt or next navbox as appropriate
    """
    if navbox.category not in ("GW", "GSW", "SW", "EDGE"):
        raise NextAttempt

    stacks = [e for e in navbox.entities if e.physical_class == IANAPhysicalClass.STACK]
    if len(stacks) == 0:
        raise NextAttempt

    root_stacks = [e for e in stacks if e.parent is None]
    if len(root_stacks) != 1:
        raise NextAttempt
    virtual_chassis = root_stacks[0]

    physical_chassises = [
        e
        for e in virtual_chassis.children
        if e.physical_class == IANAPhysicalClass.CHASSIS
    ]

    sub_stacks = [
        e
        for e in virtual_chassis.children
        if e.physical_class == IANAPhysicalClass.STACK
    ]
    for sub_stack in sub_stacks:
        _logger.warning(
            f"Stack entity {str(sub_stack)} is positioned among the physical chassis, but the "
            "parse script will not look for additional physical chassis within it; "
            "be wary of missing devices in the parsed Netbox virtual chassis"
        )

    return _parse_virtual_chassis(
        navbox, navinfo.owner_id, virtual_chassis, physical_chassises
    )


def _try_parse_juniper_virtual_chassis(
    navbox: NavBox, navinfo: NavServerInfo
) -> VirtualChassis:
    """
    Checks if navbox consists of:
    - *one* chassis entity (that will represent the virtual chassis entity)
    - *one or more* container entities (that will represent the physical chassis entities)
        that are direct children of the chassis entity, one of whom must have
        the same serial number as chassis entity.

    If so, attempts to parse as virtual chassis
    Otherwise, skips ahead to either next parse attempt or next navbox as appropriate

    The logic here is based on how Juniper EX virtual chassis switches
    report their entity hierarchy in the EntityMIB

    Notes:
    From the ex4300 docs: "When more than two switches are interconnected
                            in a virtual chassis configuration, the
                            remaining switch elements act as line cards..."

    From looking at the SQL tables in NAV and the example virtual switch
    filled into Netbox by CNaaS, it seems like switches (physical chassis)
    are represented as containers, not line cards (modules), and that all
    switches are reported as containers.
    """
    if navbox.category not in ("GW", "GSW", "SW", "EDGE"):
        raise NextAttempt

    chassises = [
        e for e in navbox.entities if e.physical_class == IANAPhysicalClass.CHASSIS
    ]
    if len(chassises) == 0:
        raise NextAttempt

    virtual_chassis = chassises[0]
    if virtual_chassis.parent is not None:
        raise NextAttempt

    if len(chassises) > 1:
        _logger.warning(f"Navbox {navbox.sysname} has multiple chassis entities")
        raise NextAttempt

    physical_chassises = [
        e
        for e in virtual_chassis.children
        if e.physical_class == IANAPhysicalClass.CONTAINER
    ]
    if len(physical_chassises) == 0:
        _logger.warning(f"Failed to find physical chassis for Navbox {navbox.sysname}")
        raise NextAttempt

    if not any(
        e.serial_number == virtual_chassis.serial_number for e in physical_chassises
    ):
        _logger.warning(
            f"Failed to find matching serial number between virtual chassis and one of its physical chassis in Navbox {navbox.sysname}"
        )
        raise NextAttempt

    return _parse_virtual_chassis(
        navbox, navinfo.owner_id, virtual_chassis, physical_chassises
    )


def _try_parse_standard_virtual_router(navbox: NavBox, navinfo: NavServerInfo):
    # Check if MAC in (00-00-5E-00-01-*, 00-00-5E-00-02-*)
    # See https://datatracker.ietf.org/doc/html/rfc5798#section-7.3
    raise NextAttempt


def _try_parse_unknown(navbox: NavBox, navinfo: NavServerInfo):
    """
    Checks if navbox
    - has incomplete data based on a propietary MIB instead of EntityMIB

    If so, attempts to ask user for additional information and parse as device
    Otherwise, skips ahead to either next parse attempt or next navbox as appropriate
    """
    if len(navbox.entities) != 1:
        raise NextAttempt

    chassis = navbox.entities[0]

    # Only continue if the chassis does not originate from EntityMIB or similar
    # sources
    if chassis.source.lower() == "entity-mib":
        raise NextAttempt
    if chassis.parent_relpos is not None:
        raise NextAttempt

    return _parse_unknown_chassis(navbox, chassis)


def _try_parse_physical_chassis(navbox: NavBox, navinfo: NavServerInfo):
    if navbox.category not in ("GW", "GSW", "SW", "EDGE"):
        raise NextAttempt
    raise NextAttempt


def _parse_unknown_chassis(navbox: NavBox, chassis: NavBoxEntity):
    raise NextAttempt([])


def _parse_virtual_chassis(
    navbox: NavBox,
    owner_id: int,
    virtual_chassis: NavBoxEntity,
    physical_chassises: list[NavBoxEntity],
) -> VirtualChassis:
    if len(physical_chassises):
        _logger.warning(f"Navbox {navbox.sysname} is missing physical chassis entities")

    for chassis in physical_chassises:
        if not chassis.fru:
            _logger.warning(
                f"Physical chassis {chassis.name} is parsed as part of a virtual chassis, despite not being field-replaceable"
            )

    devices = []
    for physical_chassis in physical_chassises:
        device = _parse_device(
            navbox,
            owner_id,
            physical_chassis,
        )
        devices.append(device)

    return VirtualChassis(
        name=navbox.sysname,
        tags=["navsync"],
        tenant=owner_id,
        devices=devices,
        description=virtual_chassis.description,
    )


def _parse_device(
    navbox: NavBox,
    owner_id: int,
    physical_chassis: NavBoxEntity,
) -> Device:
    if physical_chassis.parent_relpos is not None:
        sysname = f"{navbox.sysname}-{physical_chassis.parent_relpos}"
    else:
        sysname = navbox.sysname

    return Device(
        name=sysname,
        tags=["navsync"],
        tenant=navbox.organization_identifier,
        manufacturer=navbox.type_vendor.lower()
        if navbox.type_vendor is not None
        else None,
        model=navbox.type_name.upper() if navbox.type_name is not None else None,
        asset=_parse_asset(navbox, physical_chassis, owner_id),
        location=_parse_location(navbox, owner_id),
        role=_get_device_role_from_navbox(navbox),
        vc_position=physical_chassis.parent_relpos,
    )


def _get_device_role_from_navbox(navbox: NavBox) -> str:
    category = navbox.category
    if category == "GW":
        return "router"
    elif category == "GWS" or category == "SW":
        return "switch"
    elif category == "POWER":
        return "PDU"
    else:
        return "unknown"


def _parse_asset(navbox: NavBox, entity: NavBoxEntity, owner_id: int) -> Asset:
    return Asset(
        serial=entity.serial_number.upper()
        if entity.serial_number is not None
        else None,
        tags=["navsync"],
        manufacturer=navbox.type_vendor.lower()
        if navbox.type_vendor is not None
        else None,
        model=navbox.type_name.upper() if navbox.type_name is not None else None,
        owner=owner_id,
        tenant=navbox.organization_identifier,
    )


def _parse_location(navbox: NavBox, owner_id: int) -> Location:
    site = _parse_site(navbox, owner_id)
    return Location(
        name=navbox.room_name,
        tags=["navsync"],
        tenant=owner_id,
        description=navbox.room_description,
        site=site,
    )


def _parse_site(navbox: NavBox, owner_id: int) -> Site:
    variants = [
        "netbox_site",
        "netbox_address",
        "netbox_addr",
        "netbox_addresse",
        "address",
        "addr",
        "addresse",
        "site",
    ]

    room_data = navbox.room_data
    latitude = navbox.room_latitude
    longitude = navbox.room_longitude
    location_data = {} if navbox.room_location is None else navbox.room_location.data

    address = _get_first(room_data, variants, ignore_case=True) or _get_first(
        location_data, variants, ignore_case=True
    )
    if address:
        name = address.partition(",")[0]
        slug = "-".join(name.split()).lower()
        return Site(
            name=name,
            tags=["navsync"],
            physical_address=address,
            slug=slug,
            latitude=latitude,
            longitude=longitude,
            tenant=owner_id,
            description=navbox.room_description,
        )
    name = f"{latitude:.6f}N {longitude:.6f}E"
    slug = "-".join(name.split()).lower().replace(".", "-")
    return Site(
        name=name,
        tags=["navsync"],
        slug=slug,
        latitude=latitude,
        longitude=longitude,
        tenant=owner_id,
        description=navbox.room_description,
    )


def _get_first(d: dict, keys: list, ignore_case=False):
    if ignore_case:
        d = {k.lower(): v for k, v in d.items()}
        keys = [k.lower() for k in keys]
    for k in keys:
        if k in d:
            return d[k]


class NextAttempt(Exception):
    """
    Skip current parse method for a navbox and try the next method for the navbox

    Initialize with a list of parse methods as the first parameter
    """

    def __init__(
        self,
        include: Sequence[Callable[[NavBox, NavServerInfo], None]] | None = None,
    ):
        self.include = include
