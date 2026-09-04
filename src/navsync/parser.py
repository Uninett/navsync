import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Callable, Iterable, Literal, NewType, Optional, Self, Sequence
from urllib.parse import urljoin

import jwt
from requests.exceptions import RequestException

from navsync.geocoder import geocode_address
from navsync.nav import Api, NavBox, NavBoxEntity
from navsync.utils import NavServerInfo, sanitize_slug

_logger = logging.getLogger(__name__)

LowerStr = NewType("LowerStr", str)
ManufacturerStr = LowerStr
ModelStr = LowerStr
SlugStr = str
SerialStr = str
NameStr = str

# The NAV API endpoints navsync's token is allowed to read
NAV_TOKEN_ENDPOINTS = [
    "/api/1/netbox",
    "/api/1/netboxentity",
    "/api/1/location",
    "/api/1/room",
    "/api/1/organization",
    "/api/1/gwportprefix",
    "/api/1/interface",
]


@dataclass
class Site:
    """Information about a Netbox site instance"""

    name: str
    tags: list[str]
    slug: str
    tenant: int
    locations: dict[str, "Location"]
    nav_server: str
    status: Literal["active", "decommissioning", "planned", "retired", "staging"] = (
        "active"
    )
    latitude: float | None = None
    longitude: float | None = None
    physical_address: str | None = None
    region: int | None = None
    description: str | None = None
    comments: str | None = None
    url: Optional[str] = None


@dataclass
class Location:
    """Information about a Netbox location instance"""

    name: str
    tags: list[str]
    slug: str
    tenant: int
    site: Site
    nav_server: str
    child_locations: Optional[dict[str, "Location"]] = None
    parent_location: Optional["Location"] = None
    description: str | None = None
    comments: str | None = None
    parent: str | Self | None = None
    status: Literal["active", "decommissioning", "planned", "retired", "staging"] = (
        "active"
    )
    url: Optional[str] = None


@dataclass
class Asset:
    """Information about a Netbox asset instance"""

    tags: list[str]
    tenant: int
    owner: int
    manufacturer: ManufacturerStr
    model: ModelStr
    serial: SerialStr
    nav_server: str
    status: Literal["stored", "used", "retired"] = "used"
    contact: int | str | None = None
    navbox: NavBox | None = None
    software_version: Optional[str] = None


@dataclass
class Device:
    """Information about a Netbox device instance"""

    name: str
    tags: list[str]
    tenant: int
    location: Location
    role: Literal[
        "router", "switch", "unknown", "PDU", "server", "Uninett Environmental"
    ]
    manufacturer: ManufacturerStr
    model: ModelStr
    nav_server: str
    status: Literal[
        "active",
        "offline",
        "planned",
        "staged",
        "failed",
        "inventory",
        "decommissioning",
    ] = "active"
    asset: Asset | None = None
    vc_position: int | None = None
    navbox: NavBox | None = None
    url: Optional[str] = None


@dataclass
class VirtualChassis:
    """Information about a Netbox virtual chassis instance"""

    name: str
    tags: list[str]
    devices: list[Device]
    nav_server: str
    navbox: NavBox | None = None


@dataclass
class NavData:
    sites: list[Site] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    chassis: dict[NameStr, VirtualChassis] = field(default_factory=dict)
    devices: dict[NameStr, Device] = field(default_factory=dict)


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


class OrgTenantResolver:
    """
    Fetches the NAV organization hierarchy and resolves a tenant ID for a given
    org based on where it sits relative to the "cnaas" org.

    Rules:
    - Orgs not in the cnaas subtree are ignored (caller should skip those navboxes).
    - The "cnaas" org itself maps to the VK's default tenant.
    - Direct or indirect children of "cnaas" map to a Netbox tenant whose name
      matches the org ID, falling back to the VK's default tenant if none exists.
    """

    CNAAS_ORG = "cnaas"

    def __init__(
        self,
        nav_api: Api,
        default_tenant_id: int,
        netbox_tenants: dict[str, int],
    ):
        self._nav_api = nav_api
        self._default_tenant_id = default_tenant_id
        self._netbox_tenants = netbox_tenants
        self._parent_of: dict[str, str | None] = {}  # org_id -> parent org_id
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        for org in self._nav_api.get("organization/"):
            self._parent_of[org["id"]] = org.get("parent")
        self._loaded = True

    def _is_in_cnaas_subtree(self, org_id: str) -> bool:
        """Walk up the parent chain; return True if cnaas is found."""
        current = org_id
        while current is not None:
            if current == self.CNAAS_ORG:
                return True
            current = self._parent_of.get(current)
        return False

    def resolve(self, org_id: str) -> int | None:
        """
        Return the Netbox tenant ID for the given NAV org, or None if the org
        is not in the cnaas subtree (meaning the navbox should be skipped).
        """
        self._load()
        if not self._is_in_cnaas_subtree(org_id):
            return None
        if org_id == self.CNAAS_ORG:
            return self._default_tenant_id
        # Child of cnaas: use a tenant matching the org name if one exists.
        return self._netbox_tenants.get(org_id, self._default_tenant_id)


class EntityParser:
    def __init__(
        self,
        nav_server_info: NavServerInfo,
        token: str,
        netbox_tenants: dict[str, int],
        locations: Optional[dict[str, Location]] = None,
    ):
        self._nav_api = Api(url=nav_server_info.url, token=token)
        self._navinfo = nav_server_info
        self._token = token
        self._org_tenant_resolver = OrgTenantResolver(
            nav_api=self._nav_api,
            default_tenant_id=nav_server_info.tenant_id,
            netbox_tenants=netbox_tenants,
        )

        if locations is None:
            self._locations: dict[str, Location] = {}
        else:
            self._locations: dict[str, Location] = locations

    def parse(self) -> list[Device | VirtualChassis]:
        navboxes = self._nav_api.get_navboxes()
        if not self._locations:
            hierarchy_parser = LocationHierarchyParser(self._navinfo, self._token)
            _, self._locations = hierarchy_parser.get_sites_and_locations()

        schedule_attempt_order: list[
            Callable[[NavBox, int], Device | VirtualChassis]
        ] = [
            self._try_parse_standard_virtual_chassis,
            self._try_parse_juniper_virtual_chassis,
            self._try_parse_standard_virtual_router,
            self._try_parse_proprietary_mib,
            self._try_parse_physical_chassis,
        ]

        netbox_entities: list[Device | VirtualChassis] = []
        for navbox in navboxes:
            tenant_id = self._org_tenant_resolver.resolve(
                navbox.organization_identifier
            )
            if tenant_id is None:
                _logger.debug(
                    f"Navbox {navbox.sysname} belongs to org {navbox.organization_identifier!r} "
                    "which is not in the cnaas hierarchy. Skipping."
                )
                continue

            included_attempts = None
            for schedule_attempt in schedule_attempt_order:
                if (
                    included_attempts is not None
                    and schedule_attempt not in included_attempts
                ):
                    continue
                try:
                    netbox_entities.append(schedule_attempt(navbox, tenant_id))
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
                    continue
                else:
                    _logger.debug(f"Successfully parsed Navbox {navbox.sysname}")
                    break
            else:
                _logger.error(f"Could not parse Navbox {navbox.sysname}, skipping")
        return netbox_entities

    def _try_parse_standard_virtual_chassis(
        self, navbox: NavBox, tenant_id: int
    ) -> VirtualChassis:
        if navbox.category not in ("GW", "GSW", "SW", "EDGE"):
            raise NextAttempt

        stacks = [
            e for e in navbox.entities if e.physical_class == IANAPhysicalClass.STACK
        ]
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
        if len(physical_chassises) == 0:
            _logger.warning(
                f"Navbox {navbox.sysname} looks like a virtual chassis, but with 0 "
                f"physical chassis. Cannot parse as virtual chassis."
            )
            raise NextAttempt

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

        return self._parse_virtual_chassis(
            navbox, virtual_chassis, physical_chassises, tenant_id
        )

    def _try_parse_juniper_virtual_chassis(
        self, navbox: NavBox, tenant_id: int
    ) -> VirtualChassis:
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
            _logger.warning(
                f"Navbox {navbox.sysname} has multiple chassis entities. Cannot parse as juniper virtual chassis."
            )
            raise NextAttempt

        physical_chassises = [
            e
            for e in virtual_chassis.children
            if e.physical_class == IANAPhysicalClass.CONTAINER
        ]
        if len(physical_chassises) == 0:
            _logger.warning(
                f"Failed to find physical chassis for Navbox {navbox.sysname}. Cannot parse as juniper virtual chassis."
            )
            raise NextAttempt

        if not any(
            e.serial_number == virtual_chassis.serial_number for e in physical_chassises
        ):
            _logger.warning(
                f"Failed to find matching serial number between virtual chassis and one of its physical chassis in Navbox {navbox.sysname}. Cannot parse as juniper virtual chassis."
            )
            raise NextAttempt

        if len(physical_chassises) == 1:
            _logger.warning(
                f"Navbox {navbox.sysname} looks like a virtual chassis, but with only 1 "
                f"physical chassis. Cannot parse as juniper virtual chassis."
            )
            raise NextAttempt

        return self._parse_virtual_chassis(
            navbox, virtual_chassis, physical_chassises, tenant_id
        )

    def _try_parse_standard_virtual_router(
        self, navbox: NavBox, tenant_id: int
    ) -> None:
        raise NextAttempt

    def _try_parse_proprietary_mib(self, navbox: NavBox, tenant_id: int) -> None:
        if navbox.category not in ("GW", "GSW", "SW", "EDGE"):
            raise NextAttempt
        if len(navbox.entities) != 1:
            raise NextAttempt

        chassis = navbox.entities[0]
        if chassis.source.lower() == "entity-mib":
            raise NextAttempt
        if chassis.parent_relpos is not None:
            raise NextAttempt

        _logger.error(
            f"Navbox {navbox.sysname} has propietary mib data, which is not enough for "
            f"Navsync to decide whether it is a virtual chassis or not, and thus won't be "
            f"synced."
        )
        raise NextAttempt([])

    def _try_parse_physical_chassis(self, navbox: NavBox, tenant_id: int) -> Device:
        chassis = None
        if navbox.entities:
            physical_chassises = [
                e
                for e in navbox.entities
                if e.physical_class == IANAPhysicalClass.CHASSIS
            ]
            if len(physical_chassises) > 1:
                _logger.warning(
                    f"Found multiple physical chassis entities for Navbox {navbox.sysname}. Cannot parse as physical chassis."
                )
                raise NextAttempt
            elif len(physical_chassises) == 0:
                _logger.warning(
                    f"Failed to find physical chassis for Navbox {navbox.sysname}. Syncing without asset."
                )
            else:
                chassis = physical_chassises[0]
        else:
            _logger.warning(
                f"Failed to find entities for Navbox {navbox.sysname}. Syncing without asset."
            )
        try:
            return self._parse_device(navbox, tenant_id, chassis)
        except ValueError as err:
            _logger.warning(
                f"Failed to parse Navbox {navbox.sysname} as physical chassis: {err}"
            )
            raise NextAttempt

    def _parse_virtual_chassis(
        self,
        navbox: NavBox,
        virtual_chassis: NavBoxEntity,
        physical_chassises: list[NavBoxEntity],
        tenant_id: int,
    ) -> VirtualChassis:
        if not physical_chassises:
            _logger.warning(
                f"Navbox {navbox.sysname} is missing physical chassis entities"
            )

        for chassis in physical_chassises:
            if not chassis.fru:
                _logger.warning(
                    f"Physical chassis {chassis.name} is parsed as part of a virtual chassis, despite not being field-replaceable"
                )

        devices = []
        for physical_chassis in physical_chassises:
            if physical_chassis.parent_relpos is None:
                _logger.warning(
                    f"Failed attempt to parse {navbox.sysname} as virtual chassis: Some devices "
                    "do not have an explicit position inside the virtual chassis"
                )
                raise NextAttempt
            try:
                device = self._parse_device(
                    navbox,
                    tenant_id,
                    physical_chassis,
                    position=physical_chassis.parent_relpos,
                )
            except ValueError as err:
                _logger.warning(
                    f"Failed to parse physical chassis {physical_chassis.name} in virtual chassis {navbox.sysname} as device: {err}"
                )
                continue
            devices.append(device)

        return VirtualChassis(
            name=navbox.sysname,
            tags=["navsync"],
            devices=devices,
            nav_server=self._navinfo.url,
        )

    def _parse_device(
        self,
        navbox: NavBox,
        tenant_id: int,
        physical_chassis: Optional[NavBoxEntity] = None,
        position: Optional[int] = None,
    ) -> Device:
        if position is not None:
            sysname = self._insert_vc_position_in_sysname(navbox.sysname, position)
        else:
            sysname = navbox.sysname
        if physical_chassis:
            asset = self._parse_asset(navbox, physical_chassis, tenant_id=tenant_id)
            is_up = physical_chassis.gone_since is None and navbox.up
        else:
            asset = None
            is_up = navbox.up
        model = navbox.type_name.upper() if navbox.type_name is not None else None
        manufacturer = (
            navbox.type_vendor.lower() if navbox.type_vendor is not None else None
        )
        if not model or not manufacturer:
            raise ValueError("Missing model or manufacturer")
        url = urljoin(self._navinfo.url, f"ipdevinfo/{navbox.sysname}/")
        return Device(
            name=sysname,
            tags=["navsync"],
            tenant=tenant_id,
            manufacturer=manufacturer,
            model=model,
            nav_server=self._navinfo.url,
            asset=asset,
            location=self._locations[navbox.room_name],
            role=self._get_device_role_from_navbox(navbox),
            vc_position=position,
            status="active" if is_up else "offline",
            url=url,
        )

    @staticmethod
    def _insert_vc_position_in_sysname(sysname: str, position: int) -> str:
        """
        Adds a virtual chassis position to a sysname by inserting it before the first ".",
        e.g. sw1.c.uninett.no with position 1 becomes sw1-1.c.uninett.no. Sysnames without
        a domain part are simply suffixed.
        """
        hostname, separator, domain = sysname.partition(".")
        return f"{hostname}-{position}{separator}{domain}"

    def _get_device_role_from_navbox(self, navbox: NavBox) -> str:
        category = navbox.category
        if category == "GW":
            return "router"
        elif category == "GSW" or category == "SW":
            return "switch"
        elif category == "POWER":
            return "PDU"
        elif category == "SRV":
            return "server"
        elif category == "ENV":
            return "Uninett Environmental"
        else:
            return "unknown"

    def _parse_asset(
        self, navbox: NavBox, entity: NavBoxEntity, tenant_id: int
    ) -> Asset:
        model = navbox.type_name.upper() if navbox.type_name is not None else None
        manufacturer = (
            navbox.type_vendor.lower() if navbox.type_vendor is not None else None
        )
        if not model or not manufacturer:
            raise ValueError("Missing model or manufacturer")
        serial = (
            entity.serial_number.upper() if entity.serial_number is not None else None
        )
        if serial is None:
            raise ValueError("Missing serial number for asset")
        return Asset(
            serial=serial,
            tags=["navsync"],
            manufacturer=manufacturer,
            model=model,
            owner=self._navinfo.owner_id,
            tenant=tenant_id,
            nav_server=self._navinfo.url,
            software_version=entity.software_revision,
        )


class LocationHierarchyParser:
    """
    Helper class to parse the location hierarchy in NAV and extract Site and Location information for Netbox.
    """

    def __init__(self, nav_server_info: NavServerInfo, token: str):
        self._nav_api = Api(url=nav_server_info.url, token=token)
        self._nav_locations: dict[str, dict] = {}
        self._rooms: dict[str, dict] = {}
        self._sites: dict[str, Site] = {}
        # Map NAV location id -> Netbox Location dataclass
        self._netbox_locations: dict[str, Location] = {}
        self._navinfo: NavServerInfo = nav_server_info
        self._tenant_id: int = nav_server_info.tenant_id

    def get_sites_and_locations(self) -> tuple[dict[str, Site], dict[str, Location]]:
        self._load_nav_records()

        for room_id in self._rooms:
            room_to_root_path = self._get_room_to_root_path(room_id)
            root_location = room_to_root_path[-1]
            site = self._get_or_create_site(root_location)
            self._build_location_hierarchy(site, room_to_root_path[:-1])

        return self._sites, self._netbox_locations

    def _load_nav_records(self) -> None:
        self._nav_locations = {
            location["id"]: location for location in self._nav_api.get("location/")
        }
        self._rooms = {room["id"]: room for room in self._nav_api.get("room/")}

    def _get_room_to_root_path(self, room: str) -> list[dict]:
        """
        Returns a list representing the path from a room to the root location in NAV,
        starting with the room and ending with the root location.
        """
        if room not in self._rooms:
            raise ValueError(f"Room {room} not found in NAV")

        path: list[dict] = []
        room_record = self._rooms[room]
        path.append(room_record)

        location_id = room_record.get("location")
        while location_id is not None:
            nav_location = self._nav_locations.get(location_id)
            if nav_location is None:
                break
            path.append(nav_location)

            data = nav_location.get("data") or {}
            if data.get("addr"):
                break
            if nav_location.get("parent") is None:
                break
            location_id = nav_location.get("parent")

        if len(path) == 1:
            raise ValueError(f"No location hierarchy found for room {room}")

        return path

    def _build_location_hierarchy(
        self, site: Site, location_path: list[dict]
    ) -> Location | None:
        parent_location: Location | None = None
        # location_path[0] is the room (leaf node); iterate reversed to build top-down
        for i, nav_location in enumerate(reversed(location_path)):
            name = nav_location["id"]
            if name in self._netbox_locations:
                netbox_location = self._netbox_locations[name]
            else:
                # Last item in the reversed loop is location_path[0] — the room.
                # Use index rather than name since rooms and locations can share names.
                is_room = i == len(location_path) - 1
                api_segment = "room" if is_room else "location"
                netbox_location = Location(
                    name=name,
                    tags=["navsync"],
                    slug=sanitize_slug(name),
                    tenant=self._tenant_id,
                    site=site,
                    nav_server=self._navinfo.url,
                    parent_location=parent_location,
                    child_locations={},
                    description=nav_location.get("description"),
                    url=urljoin(self._navinfo.url, f"search/{api_segment}/{name}/"),
                )
                self._netbox_locations[name] = netbox_location

                if parent_location is None:
                    site.locations[netbox_location.name] = netbox_location
                else:
                    if parent_location.child_locations is None:
                        parent_location.child_locations = {}
                    parent_location.child_locations[netbox_location.name] = (
                        netbox_location
                    )

            parent_location = netbox_location
        return parent_location

    def _get_or_create_site(self, location_data: dict) -> Site:
        name = location_data["id"]
        if name in self._sites:
            return self._sites[name]
        physical_address = (location_data.get("data") or {}).get("addr")
        try:
            coords = geocode_address(physical_address) if physical_address else None
        except RequestException as e:
            _logger.error("Could not get geocode address: %s", e)
            coords = None

        site = Site(
            name=name,
            tags=["navsync"],
            slug=sanitize_slug(name),
            tenant=self._navinfo.tenant_id,
            locations={},
            nav_server=self._navinfo.url,
            description=location_data.get("description"),
            physical_address=physical_address,
            latitude=coords[0] if coords else None,
            longitude=coords[1] if coords else None,
            url=urljoin(self._navinfo.url, f"search/location/{name}/"),
        )
        self._sites[name] = site
        return site


class NavFetcher:
    """
    Fetches sites, locations, devices and virtual chassis from NAV instances.

    Use :meth fetch: to get everything from a set of instances in a single pass.
    """

    def __init__(
        self,
        private_key: str,
        expiry_delta: timedelta,
        issuer: str,
        netbox_tenants: dict[NameStr, int],
    ):
        """
        :param private_key: PEM-encoded RSA key used to sign tokens for the NAV
            API. The corresponding public key must be registered with the NAV
            instances to be accessed.
        :param expiry_delta: how long a signed token stays valid
        :param issuer: the 'iss' claim to set on signed tokens, which must match
            what the NAV instances are configured with
        :param netbox_tenants: maps Netbox tenant name to id, used to resolve
            which tenant a navbox's organization belongs to
        """
        self.private_key = private_key
        self.expiry_delta = expiry_delta
        self.issuer = issuer
        self.netbox_tenants = netbox_tenants

    def fetch(self, nav_servers: Iterable[NavServerInfo]) -> NavData:
        """
        Fetches all data from the given NAV servers in a single pass.

        :param nav_servers: the NAV instances to fetch from
        """
        result = NavData()
        sites: dict[str, Site] = {}  # keyed by name for duplicate detection
        all_entities: list[VirtualChassis | Device] = []

        for nav_server in nav_servers:
            token = self.generate_nav_token(aud=nav_server.url)
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
                    netbox_tenants=self.netbox_tenants,
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

    def generate_nav_token(self, aud: str) -> str:
        """Returns a JWT granting read access to the NAV API at 'aud'"""
        now = datetime.now(timezone.utc)
        jwt_claims = {
            "exp": (now + self.expiry_delta).timestamp(),
            "nbf": now.timestamp(),
            "iat": now.timestamp(),
            "aud": aud,
            "iss": self.issuer,
            "token_type": "access",
            "endpoints": NAV_TOKEN_ENDPOINTS,
            "write": False,
        }
        return jwt.encode(jwt_claims, self.private_key, algorithm="RS256")


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
