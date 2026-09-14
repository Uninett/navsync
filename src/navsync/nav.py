import dataclasses
import datetime as dt
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Iterable, Literal, Optional, Self

import requests

_logger = getLogger(__name__)


@dataclass
class NavLocation:
    """Represents a 'location' model instance from Nav"""

    _id: str
    _parent_id: Optional[str]

    name: str
    description: str
    data: dict[str, Optional[str]]

    parent: Optional[Self] = dataclasses.field(repr=False)
    children: list[Self] = dataclasses.field(repr=False)


@dataclass
class NavBoxEntity:
    """Represents a 'netbox entity' model instance from Nav"""

    _id: int
    _navbox_id: int
    _parent_id: Optional[int]

    name: str
    description: str
    source: str
    physical_class: Optional[int]
    fru: Optional[bool]
    parent_relpos: Optional[int]
    serial_number: Optional[str]
    software_revision: Optional[str]

    gone_since: Optional[dt.datetime]
    # discovered: dt.datetime #TODO: NOT YET INCLUDED THE NAV API BUT USEFUL FOR ESTIMATING DATE OF PURCHASE; need to add this from models.Device to the models.NetboxEntity NAV API endpoint

    parent: Optional[Self] = dataclasses.field(repr=False)
    children: list[Self] = dataclasses.field(repr=False)


@dataclass
class NavGwPortPrefix:
    """
    Represents a 'gwportprefix' model instance from Nav, i.e. an IP address
    assigned to an interface.
    """

    # The IP address assigned to the interface, without a mask
    ip: str
    # The prefix the IP address belongs to, e.g. '10.0.0.0/24'. Used to derive
    # the mask length for the IP address in Netbox.
    prefix: Optional[str]
    virtual: bool


@dataclass
class NavInterface:
    """
    Represents an 'interface' model instance from Nav, together with the IP
    addresses assigned to it via 'gwportprefix' model instances.

    Only interfaces that have at least one gwportprefix entry are represented,
    since an interface without an IP address is of no interest to Netbox.
    """

    _id: int
    _navbox_id: int

    name: str
    ifindex: Optional[int]
    description: str

    # Every IP address assigned to this interface, as reported by gwportprefix.
    # An interface commonly has both an IPv4 and an IPv6 address.
    addresses: list[NavGwPortPrefix]


@dataclass
class NavBox:
    """Represents a 'netbox' model instance from Nav"""

    _id: int
    _room_location_id: str

    sysname: str
    ip: str
    category: Literal["GW", "GSW", "SW", "EDGE", "WLAN", "SRV", "OTHER", "ENV", "POWER"]
    entities: list[NavBoxEntity]
    #    mfg_date: Optional[datetime]

    type_name: Optional[str]
    type_description: Optional[str]
    type_vendor: Optional[str]

    room_name: str
    room_latitude: Optional[float]
    room_longitude: Optional[float]
    room_description: str
    room_data: dict[str, Optional[str]]
    room_location: Optional[NavLocation]

    # Every interface on this navbox that has at least one IP address assigned
    # to it, as reported by Nav's gwportprefix data. The interface holding the
    # navbox's management IP (NavBox.ip) is among these, if it could be resolved.
    interfaces: list[NavInterface]

    organization_identifier: str
    organization_description: str
    organization_contact: str
    organization_data: dict[str, Optional[str]]

    up: bool

    def __str__(self):
        infomap = (
            ("sysname", self.sysname),
            ("category", self.category),
            ("type_name", self.type_name),
            ("organization", self.organization_identifier),
        )
        info = ", ".join(f"{key}={value}" for key, value in infomap if value)
        return f"NavBox({info})"


class Api:
    """Token-based access to Nav's API"""

    def __init__(self, url: str, token: str):
        self.base_url = url.rstrip("/") + "/api/1/"
        self.session = requests.Session()
        self.session.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def get(self, path: str, params=None) -> Iterable[dict]:
        """Sends a GET request to Nav's API"""
        url = self.base_url + path.lstrip("/")
        while url:
            try:
                response = self.session.get(url, params=params)
                # Nav's 'next' link already contains the query parameters, so
                # they must not be sent again for the subsequent pages
                params = None
                response.raise_for_status()
                response = response.json()
            except (requests.RequestException, requests.JSONDecodeError) as err:
                raise ConnectionError(*err.args)
            if "results" not in response:
                raise AuthenticationError(url)
            yield from response["results"]
            url = response.get("next", None)

    def get_single(self, path: str, params=None) -> Optional[dict]:
        """
        Sends a GET request to a detail route in Nav's API and returns the
        single object it refers to, or None if it does not exist.

        Unlike :meth get:, this expects an un-paginated response containing a
        single object rather than a list of results.

        Nav applies its regular filters to detail routes too, so 'params' can
        be used to constrain which object is considered a match. An object that
        does not match is reported as not existing.
        """
        url = self.base_url + path.lstrip("/")
        try:
            response = self.session.get(url, params=params)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            json = response.json()
        except (requests.RequestException, requests.JSONDecodeError) as err:
            raise ConnectionError(*err.args)
        if not isinstance(json, dict):
            raise ConnectionError(f"Unexpected response from {url}: {json!r}")
        # A detail route returns the object itself, so a 'results' key means we
        # were served a list endpoint, which in turn means the token was not
        # accepted and we were redirected to the API root
        if "results" in json:
            raise AuthenticationError(url)
        return json

    def get_navboxes(self) -> list[NavBox]:
        """
        Return a list of NavBox objects, one per 'netbox' model instance from Nav,
        with all relations to other Nav model representation fully filled out.
        """

        start = time.time()
        box_by_id = self._get_unfilled_navboxes_by_id()
        entity_by_id = self._get_unfilled_navboxentities_by_id()
        location_by_id = self._get_unfilled_locations_by_id()
        end = time.time()
        _logger.debug(
            "Getting all models from %s took %.6f seconds", self.base_url, end - start
        )

        start = time.time()
        # fill in relations between boxes in 'box_by_id' and entities in 'entity_by_id'
        # and fill in relations between entities in 'entity_by_id' and other entities in 'entity_by_id'
        for entity in entity_by_id.values():
            box_by_id[entity._navbox_id].entities.append(entity)
            if entity._parent_id is not None:
                parent = entity_by_id[entity._parent_id]
                parent.children.append(entity)
                entity.parent = parent

        # fill in relations between locations in 'location_by_id'
        for location in location_by_id.values():
            if location._parent_id is not None:
                parent = location_by_id[location._parent_id]
                parent.children.append(location)
                location.parent = parent

        # fill in relations between boxes in 'box_by_id' and locations in 'location_by_id'
        for box in box_by_id.values():
            box.room_location = location_by_id[box._room_location_id]

        end = time.time()
        _logger.debug(
            "Filling relations for all models from %s took %.6f seconds",
            self.base_url,
            end - start,
        )

        # Fill in each box's interfaces that have IP addresses assigned to them.
        # This is looked up per box, since fetching all gateway port prefixes
        # and interfaces would mean paging through far more data than is needed.
        start = time.time()
        for box in box_by_id.values():
            box.interfaces = self._get_interfaces_with_addresses(box._id)
            if not box.interfaces:
                _logger.debug(
                    "Found no interfaces with IP addresses in %s for navbox %s",
                    self.base_url,
                    box.sysname,
                )
        end = time.time()
        _logger.debug(
            "Getting interfaces with IP addresses for all netboxes from %s "
            "took %.6f seconds",
            self.base_url,
            end - start,
        )

        return list(box_by_id.values())

    def _get_unfilled_navboxes_by_id(self) -> dict[int, NavBox]:
        """
        Get all 'netbox' model instances from Nav and return a map from their ids to
        their NavBox representation.

        Any reference in NavBox to other Nav objects is not filled in and is
        set to None (or an empty list, etc.)
        """

        box_by_id = {}
        model_instance_json_list = self.get("netbox/")
        for json in model_instance_json_list:
            box = NavBox(
                _id=json["id"],
                _room_location_id=json["room"]["location"],
                room_location=None,
                interfaces=[],
                entities=[],
                sysname=json["sysname"],
                ip=json.get("ip") or "",
                category=json["category"]["id"],
                type_name=None if json["type"] is None else json["type"]["name"],
                type_description=None
                if json["type"] is None
                else json["type"]["description"],
                type_vendor=None if json["type"] is None else json["type"]["vendor"],
                room_name=json["room"]["id"],
                room_description=json["room"]["description"],
                room_latitude=float(json["room"]["position"][0])
                if json["room"].get("position")
                else None,
                room_longitude=float(json["room"]["position"][1])
                if json["room"].get("position")
                else None,
                room_data=json["room"]["data"],
                organization_identifier=json["organization"]["id"],
                organization_description=json["organization"]["description"],
                organization_contact=json["organization"]["contact"],
                organization_data=json["organization"]["data"],
                up=True if json["up"] == "y" else False,
            )
            box_by_id[json["id"]] = box
        return box_by_id

    def _get_unfilled_navboxentities_by_id(self) -> dict[int, NavBoxEntity]:
        """
        Get all 'netbox entity' model instances from Nav and return a map from their ids
        to their NavBoxEntity representation.

        Any reference in NavBoxEntity to other Nav objects is not filled in and
        is set to None (or an empty list, etc.)
        """
        entity_by_id = {}
        model_instance_json_list = self.get("netboxentity/")
        for json in model_instance_json_list:
            entity = NavBoxEntity(
                _id=json["id"],
                _parent_id=json["contained_in"],
                _navbox_id=json["netbox"],
                gone_since=(datestr := json["gone_since"])
                and dt.datetime.fromisoformat(datestr),
                parent=None,
                children=[],
                physical_class=json["physical_class"],
                parent_relpos=json["parent_relpos"],
                serial_number=(json["device"] or {}).get("serial", None),
                description=json["descr"] or "",
                name=json["name"] or "",
                source=json["source"],
                fru=json["fru"],
                software_revision=json.get("software_revision"),
            )
            entity_by_id[json["id"]] = entity
        return entity_by_id

    def _get_interfaces_with_addresses(self, navbox_id: int) -> list[NavInterface]:
        """
        Return every interface on the given netbox that has at least one IP
        address assigned to it, according to Nav's gwportprefix data.

        Interfaces without a gwportprefix entry are deliberately not included:
        an interface with no IP address has nothing to contribute to Netbox, and
        a switch can have hundreds of them.

        :param navbox_id: the id of the netbox to get interfaces for
        """
        # One request yields every IP address on the netbox, grouped by the
        # interface it belongs to. Filtering on the netbox also means Nav will
        # never hand us an interface belonging to some other netbox.
        addresses_by_interface_id: dict[int, list[NavGwPortPrefix]] = {}
        ifindex_by_interface_id: dict[int, Optional[int]] = {}
        for json in self.get("gwportprefix/", params={"interface__netbox": navbox_id}):
            interface_json = json.get("interface") or {}
            interface_id = interface_json.get("id")
            gw_ip = json.get("gw_ip")
            if interface_id is None or gw_ip is None:
                _logger.debug(
                    "Skipping gwportprefix from %s with incomplete data: %r",
                    self.base_url,
                    json,
                )
                continue

            # gw_ip may be reported with a mask, e.g. '10.0.0.1/24', but the
            # management IP in NavBox.ip never is, so normalize it here to make
            # the two comparable
            ip = gw_ip.split("/")[0]
            addresses_by_interface_id.setdefault(interface_id, []).append(
                NavGwPortPrefix(
                    ip=ip,
                    prefix=(json.get("prefix") or {}).get("net_address"),
                    virtual=bool(json.get("virtual", False)),
                )
            )
            ifindex_by_interface_id[interface_id] = interface_json.get("ifindex")

        interfaces = []
        for interface_id, addresses in addresses_by_interface_id.items():
            # The gwportprefix only inlines the interface's id, ifindex and
            # netbox, so the name has to be looked up from the interface itself
            interface_json = (
                self.get_single(
                    f"interface/{interface_id}/", params={"netbox": navbox_id}
                )
                or {}
            )
            name = interface_json.get("ifname")
            if not name:
                _logger.warning(
                    "Interface %s in %s has no ifname, cannot sync the IP "
                    "address(es) %s assigned to it",
                    interface_id,
                    self.base_url,
                    ", ".join(address.ip for address in addresses),
                )
                continue

            interfaces.append(
                NavInterface(
                    _id=interface_id,
                    _navbox_id=navbox_id,
                    name=name,
                    ifindex=ifindex_by_interface_id.get(interface_id),
                    description=interface_json.get("ifalias")
                    or interface_json.get("ifdescr")
                    or "",
                    addresses=addresses,
                )
            )
        return interfaces

    def _get_unfilled_locations_by_id(self) -> dict[str, NavLocation]:
        """
        Get all 'location' model instances from Nav and return a map from their ids to
        their NavOrganization representation.

        Any reference in NavLocation to other Nav objects is not filled in and is set to
        None (or an empty list, etc.)
        """
        location_by_id = {}
        model_instance_json_list = self.get("location/")
        for json in model_instance_json_list:
            location = NavLocation(
                _id=json["id"],
                _parent_id=json["parent"],
                parent=None,
                children=[],
                name=json["id"],
                description=json["description"],
                data=json["data"],
            )
            location_by_id[json["id"]] = location
        return location_by_id


class NavError(Exception):
    """
    An error occurred while talking to a Nav instance
    """

    def __str__(self):
        args = Exception.__str__(self)
        docstring = " ".join(
            filter(bool, map(str.strip, (self.__doc__ or "...").split("\n")))
        )
        if args:
            return f"{docstring}: {args}"
        return docstring


class ConnectionError(NavError):
    """
    Some error occurred while connecting, or during the connection, to a Nav
    instance endpoint
    """


class AuthenticationError(ConnectionError):
    """
    Failed to authenticate to a Nav instance endpoint
    """
