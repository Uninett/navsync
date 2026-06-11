"""
Tests for LocationHierarchyParser.

Terminology used in these tests:
  - NAV location: a location record fetched from the NAV API (plain dict)
  - NAV room: a leaf node in the NAV hierarchy (plain dict)
  - Netbox Site: the root of the hierarchy, parsed from the root NAV location
  - Netbox Location: an intermediate or leaf node, parsed from NAV locations and rooms
"""

from unittest.mock import MagicMock

import pytest

from navsync.parser import LocationHierarchyParser
from navsync.utils import NavServerInfo


def make_parser(nav_server, nav_locations, nav_rooms):
    """Return a LocationHierarchyParser whose API calls are mocked."""
    parser = LocationHierarchyParser(nav_server, token="test-token")
    parser._nav_api = MagicMock()
    parser._nav_api.get.side_effect = lambda endpoint: (
        nav_locations
        if endpoint == "location/"
        else nav_rooms
        if endpoint == "room/"
        else []
    )
    return parser


def nav_location(id, parent=None, addr=None, description=None):
    """Build a minimal NAV location dict."""
    data = {"addr": addr} if addr else None
    return {"id": id, "parent": parent, "data": data, "description": description}


def nav_room(id, location, description=None):
    """Build a minimal NAV room dict."""
    return {"id": id, "location": location, "description": description}


class TestSimpleHierarchy:
    """Root NAV location → intermediate NAV location → room."""

    def test_root_nav_location_should_become_a_site(self, simple_sites):
        assert "root_loc" in simple_sites

    def test_site_should_have_the_address_of_the_root_nav_location(self, simple_sites):
        assert simple_sites["root_loc"].physical_address == "1 Main St"

    def test_site_should_have_the_navsync_tag(self, simple_sites):
        assert "navsync" in simple_sites["root_loc"].tags

    def test_site_tenant_should_match_nav_server_tenant(self, simple_sites, nav_server):
        assert simple_sites["root_loc"].tenant == nav_server.tenant_id

    def test_site_url_should_point_to_nav_location_search(self, simple_sites):
        assert (
            simple_sites["root_loc"].url
            == "http://nav.example.com/search/location/root_loc/"
        )

    def test_intermediate_nav_location_should_become_a_netbox_location(
        self, simple_netbox_locations
    ):
        assert "mid_loc" in simple_netbox_locations

    def test_nav_room_should_become_a_netbox_location(self, simple_netbox_locations):
        assert "room1" in simple_netbox_locations

    def test_netbox_location_site_should_match_the_parsed_site(
        self, simple_sites, simple_netbox_locations
    ):
        assert simple_netbox_locations["mid_loc"].site is simple_sites["root_loc"]

    def test_room_netbox_location_site_should_match_the_parsed_site(
        self, simple_sites, simple_netbox_locations
    ):
        assert simple_netbox_locations["room1"].site is simple_sites["root_loc"]

    def test_when_nav_location_is_child_to_root_then_it_should_not_have_a_parent_location(
        self, simple_netbox_locations
    ):
        assert simple_netbox_locations["mid_loc"].parent_location is None

    def test_when_room_is_under_intermediate_location_then_parent_should_be_that_location(
        self, simple_netbox_locations
    ):
        assert (
            simple_netbox_locations["room1"].parent_location
            is simple_netbox_locations["mid_loc"]
        )

    def test_intermediate_netbox_location_url_should_use_location_segment(
        self, simple_netbox_locations
    ):
        assert (
            simple_netbox_locations["mid_loc"].url
            == "http://nav.example.com/search/location/mid_loc/"
        )

    def test_room_netbox_location_url_should_use_room_segment(
        self, simple_netbox_locations
    ):
        assert (
            simple_netbox_locations["room1"].url
            == "http://nav.example.com/search/room/room1/"
        )

    def test_netbox_location_should_be_listed_in_site_locations(self, simple_sites):
        assert "mid_loc" in simple_sites["root_loc"].locations

    def test_room_netbox_location_should_be_listed_in_parent_child_locations(
        self, simple_netbox_locations
    ):
        assert "room1" in simple_netbox_locations["mid_loc"].child_locations


class TestRootDetection:
    """The root NAV location is determined by addr field or top of hierarchy."""

    def test_when_ancestor_has_addr_then_it_should_become_the_site_not_the_topmost_location(
        self, nav_server
    ):
        # root_loc has addr → should be the site even though top_loc exists above it
        nav_locs = [
            nav_location("top_loc", parent=None),
            nav_location("root_loc", parent="top_loc", addr="42 Some St"),
            nav_location("mid_loc", parent="root_loc"),
        ]
        nav_rooms = [nav_room("room1", location="mid_loc")]
        sites, netbox_locations = make_parser(
            nav_server, nav_locs, nav_rooms
        ).get_sites_and_locations()

        assert "root_loc" in sites
        assert "top_loc" not in sites
        assert "top_loc" not in netbox_locations

    def test_when_no_nav_location_has_addr_then_topmost_should_become_the_site(
        self, nav_server
    ):
        nav_locs = [
            nav_location("top_loc", parent=None),
            nav_location("mid_loc", parent="top_loc"),
        ]
        nav_rooms = [nav_room("room1", location="mid_loc")]
        sites, _ = make_parser(
            nav_server, nav_locs, nav_rooms
        ).get_sites_and_locations()

        assert "top_loc" in sites
        assert sites["top_loc"].physical_address is None


class TestSharedLocations:
    """Two rooms sharing an intermediate NAV location should not duplicate Netbox Locations."""

    def test_when_two_rooms_share_a_nav_location_then_it_should_only_be_created_once(
        self, shared_location_result
    ):
        _, netbox_locations = shared_location_result
        assert len([k for k in netbox_locations if k == "mid_loc"]) == 1

    def test_when_two_rooms_share_a_nav_location_then_both_rooms_should_be_present(
        self, shared_location_result
    ):
        _, netbox_locations = shared_location_result
        assert "room_a" in netbox_locations
        assert "room_b" in netbox_locations


class TestMultipleSites:
    """Rooms under different root NAV locations produce separate Netbox Sites."""

    def test_when_two_root_nav_locations_exist_then_two_sites_should_be_created(
        self, two_sites_result
    ):
        sites, _ = two_sites_result
        assert "site_a" in sites
        assert "site_b" in sites

    def test_when_rooms_are_under_different_root_locations_then_they_should_belong_to_different_sites(
        self, two_sites_result
    ):
        sites, netbox_locations = two_sites_result
        assert netbox_locations["room_a"].site is sites["site_a"]
        assert netbox_locations["room_b"].site is sites["site_b"]


class TestDescriptions:
    def test_site_description_should_come_from_root_nav_location(self, nav_server):
        nav_locs = [
            nav_location("root_loc", parent=None, addr="1 St", description="HQ")
        ]
        nav_rooms = [nav_room("room1", location="root_loc")]
        sites, _ = make_parser(
            nav_server, nav_locs, nav_rooms
        ).get_sites_and_locations()

        assert sites["root_loc"].description == "HQ"

    def test_netbox_location_description_should_come_from_nav_room(self, nav_server):
        nav_locs = [nav_location("root_loc", parent=None, addr="1 St")]
        nav_rooms = [nav_room("room1", location="root_loc", description="Server room")]
        _, netbox_locations = make_parser(
            nav_server, nav_locs, nav_rooms
        ).get_sites_and_locations()

        assert netbox_locations["room1"].description == "Server room"


class TestErrorCases:
    def test_when_room_has_no_ancestor_nav_location_then_it_should_raise(
        self, nav_server
    ):
        nav_locs = []
        nav_rooms = [nav_room("orphan_room", location="nonexistent_loc")]

        with pytest.raises(ValueError, match="No location hierarchy found"):
            make_parser(nav_server, nav_locs, nav_rooms).get_sites_and_locations()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nav_server():
    return NavServerInfo(id=1, url="http://nav.example.com/", owner_id=10, tenant_id=42)


@pytest.fixture
def simple_hierarchy_result(nav_server):
    nav_locs = [
        nav_location("root_loc", parent=None, addr="1 Main St"),
        nav_location("mid_loc", parent="root_loc"),
    ]
    nav_rooms = [nav_room("room1", location="mid_loc")]
    return make_parser(nav_server, nav_locs, nav_rooms).get_sites_and_locations()


@pytest.fixture
def simple_sites(simple_hierarchy_result):
    sites, _ = simple_hierarchy_result
    return sites


@pytest.fixture
def simple_netbox_locations(simple_hierarchy_result):
    _, netbox_locations = simple_hierarchy_result
    return netbox_locations


@pytest.fixture
def shared_location_result(nav_server):
    nav_locs = [
        nav_location("root_loc", parent=None, addr="1 Main St"),
        nav_location("mid_loc", parent="root_loc"),
    ]
    nav_rooms = [
        nav_room("room_a", location="mid_loc"),
        nav_room("room_b", location="mid_loc"),
    ]
    return make_parser(nav_server, nav_locs, nav_rooms).get_sites_and_locations()


@pytest.fixture
def two_sites_result(nav_server):
    nav_locs = [
        nav_location("site_a", parent=None, addr="1 A St"),
        nav_location("site_b", parent=None, addr="2 B St"),
    ]
    nav_rooms = [
        nav_room("room_a", location="site_a"),
        nav_room("room_b", location="site_b"),
    ]
    return make_parser(nav_server, nav_locs, nav_rooms).get_sites_and_locations()
