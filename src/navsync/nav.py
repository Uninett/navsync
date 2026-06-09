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
class NavBox:
    """Represents a 'netbox' model instance from Nav"""

    _id: int
    _room_location_id: str

    sysname: str
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
                response.raise_for_status()
                response = response.json()
            except (requests.RequestException, requests.JSONDecodeError) as err:
                raise ConnectionError(*err.args)
            if "results" not in response:
                raise AuthenticationError(url)
            yield from response["results"]
            url = response.get("next", None)

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
                entities=[],
                sysname=json["sysname"],
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
