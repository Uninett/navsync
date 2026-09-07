from unittest.mock import MagicMock

import pytest
import requests

from navsync.nav import (
    Api,
    AuthenticationError,
    ConnectionError,
    NavGwPortPrefix,
    NavInterface,
)
from navsync.parser import EntityParser
from navsync.utils import NavServerInfo

GWP_PARAMS = {"interface__netbox": 10}
IFACE_PARAMS = {"netbox": 10}


def gwportprefix(gw_ip, interface_id=1, navbox_id=10, net_address=None, virtual=False):
    return {
        "gw_ip": gw_ip,
        "virtual": virtual,
        "interface": {"id": interface_id, "ifindex": 501, "netbox": navbox_id},
        "prefix": {"id": 7, "net_address": net_address, "vlan": 3}
        if net_address
        else None,
    }


def interface(id=1, ifname="Vlan10", ifalias="", ifdescr=""):
    return {"id": id, "ifname": ifname, "ifalias": ifalias, "ifdescr": ifdescr}


def make_api(gwportprefixes, interfaces=None, iface_params=IFACE_PARAMS):
    """
    Builds an Api whose `get` returns the given gwportprefixes and whose
    `get_single` returns the given interfaces keyed by id.
    """
    api = Api.__new__(Api)
    api.base_url = "http://nav.example.org/api/1/"
    api.get = MagicMock(side_effect=lambda path, params=None: iter(gwportprefixes))

    interfaces = interfaces or {}

    def get_single(path, params=None):
        if iface_params is not None and params != iface_params:
            return None
        interface_id = int(path.rstrip("/").split("/")[-1])
        return interfaces.get(interface_id)

    api.get_single = MagicMock(side_effect=get_single)
    return api


class TestGetInterfacesWithAddresses:
    def test_interface_with_one_address_should_be_returned(self):
        api = make_api(
            [gwportprefix("10.0.0.1", net_address="10.0.0.0/24")],
            {1: interface(ifname="Vlan10")},
        )
        result = api._get_interfaces_with_addresses(10)
        assert len(result) == 1
        assert result[0].name == "Vlan10"
        assert [a.ip for a in result[0].addresses] == ["10.0.0.1"]
        assert result[0].addresses[0].prefix == "10.0.0.0/24"

    def test_ipv4_and_ipv6_on_one_interface_should_both_be_returned(self):
        api = make_api(
            [
                gwportprefix("10.0.0.1", interface_id=1, net_address="10.0.0.0/24"),
                gwportprefix(
                    "2001:db8::1", interface_id=1, net_address="2001:db8::/64"
                ),
            ],
            {1: interface(ifname="Vlan10")},
        )
        result = api._get_interfaces_with_addresses(10)
        assert len(result) == 1, "both addresses belong to the same interface"
        assert [a.ip for a in result[0].addresses] == ["10.0.0.1", "2001:db8::1"]

    def test_addresses_on_separate_interfaces_should_produce_separate_interfaces(self):
        api = make_api(
            [
                gwportprefix("10.0.0.1", interface_id=1),
                gwportprefix("10.0.1.1", interface_id=2),
            ],
            {1: interface(id=1, ifname="Vlan10"), 2: interface(id=2, ifname="Vlan20")},
        )
        result = api._get_interfaces_with_addresses(10)
        assert sorted(i.name for i in result) == ["Vlan10", "Vlan20"]

    def test_the_gwportprefix_request_should_be_filtered_on_the_netbox(self):
        api = make_api([gwportprefix("10.0.0.1")], {1: interface()})
        api._get_interfaces_with_addresses(10)
        assert api.get.call_args.args[0] == "gwportprefix/"
        assert api.get.call_args.kwargs["params"] == {"interface__netbox": 10}

    def test_the_interface_request_should_be_filtered_on_the_netbox(self):
        api = make_api([gwportprefix("10.0.0.1")], {1: interface()})
        api._get_interfaces_with_addresses(10)
        assert api.get_single.call_args.kwargs["params"] == {"netbox": 10}

    def test_each_interface_should_only_be_looked_up_once(self):
        api = make_api(
            [
                gwportprefix("10.0.0.1", interface_id=1),
                gwportprefix("2001:db8::1", interface_id=1),
            ],
            {1: interface()},
        )
        api._get_interfaces_with_addresses(10)
        assert api.get_single.call_count == 1

    def test_navbox_without_gwportprefixes_should_produce_no_interfaces(self):
        api = make_api([], {1: interface()})
        assert api._get_interfaces_with_addresses(10) == []
        api.get_single.assert_not_called()

    def test_gwportprefix_with_incomplete_data_should_be_skipped(self):
        api = make_api([{"gw_ip": "10.0.0.1", "interface": None}], {1: interface()})
        assert api._get_interfaces_with_addresses(10) == []

    def test_address_should_expose_the_prefix_it_belongs_to(self):
        api = make_api(
            [gwportprefix("10.130.12.1", net_address="10.130.12.0/22")],
            {1: interface()},
        )
        result = api._get_interfaces_with_addresses(10)
        assert result[0].addresses[0].prefix == "10.130.12.0/22"

    def test_gwportprefix_without_a_prefix_should_have_no_prefix(self):
        api = make_api([gwportprefix("10.130.12.1")], {1: interface()})
        result = api._get_interfaces_with_addresses(10)
        assert result[0].addresses[0].prefix is None

    def test_gw_ip_reported_with_a_mask_should_be_normalized(self):
        api = make_api([gwportprefix("10.0.0.1/24")], {1: interface()})
        result = api._get_interfaces_with_addresses(10)
        assert [a.ip for a in result[0].addresses] == ["10.0.0.1"]

    def test_interface_without_ifname_should_be_skipped(self):
        api = make_api([gwportprefix("10.0.0.1")], {1: interface(ifname="")})
        assert api._get_interfaces_with_addresses(10) == []

    def test_interface_on_a_different_navbox_should_be_skipped(self):
        api = make_api(
            [gwportprefix("10.0.0.1")],
            {1: interface()},
            iface_params={"netbox": 20},
        )
        assert api._get_interfaces_with_addresses(10) == []

    def test_description_should_prefer_ifalias_over_ifdescr(self):
        api = make_api(
            [gwportprefix("10.0.0.1")],
            {1: interface(ifalias="uplink", ifdescr="some descr")},
        )
        result = api._get_interfaces_with_addresses(10)
        assert result[0].description == "uplink"

    def test_ifdescr_should_be_used_when_there_is_no_ifalias(self):
        api = make_api(
            [gwportprefix("10.0.0.1")],
            {1: interface(ifalias="", ifdescr="some descr")},
        )
        result = api._get_interfaces_with_addresses(10)
        assert result[0].description == "some descr"


def address(ip="158.38.1.13", prefix="158.38.1.0/24"):
    return NavGwPortPrefix(ip=ip, prefix=prefix, virtual=False)


class TestIpWithMask:
    """Addresses are always registered as single-host addresses"""

    def test_address_should_get_a_host_mask(self):
        assert EntityParser._ip_with_mask(address(prefix=None)) == "158.38.1.13/32"

    def test_prefix_mask_length_should_be_ignored(self):
        """The prefix an address sits in is not expressed on the address"""
        assert (
            EntityParser._ip_with_mask(address(prefix="158.38.1.0/24"))
            == "158.38.1.13/32"
        )

    def test_ipv6_address_should_get_a_host_mask(self):
        result = EntityParser._ip_with_mask(address(ip="2001:700::1", prefix=None))
        assert result == "2001:700::1/128"

    def test_ipv6_prefix_mask_length_should_be_ignored(self):
        result = EntityParser._ip_with_mask(
            address(ip="2001:700::1", prefix="2001:700::/64")
        )
        assert result == "2001:700::1/128"


def make_parser():
    parser = EntityParser.__new__(EntityParser)
    parser._navinfo = NavServerInfo(
        id=1, url="http://nav.example.org/", owner_id=1, tenant_id=1
    )
    return parser


def nav_interface(name="Vlan10", addresses=None, description=""):
    return NavInterface(
        _id=1,
        _navbox_id=10,
        name=name,
        ifindex=501,
        description=description,
        addresses=addresses if addresses is not None else [address()],
    )


def navbox(ip="158.38.1.13", interfaces=None):
    box = MagicMock()
    box.sysname = "sw-1.example.org"
    box.ip = ip
    box.interfaces = interfaces if interfaces is not None else [nav_interface()]
    return box


class TestParseInterfaces:
    def test_interface_should_be_parsed_with_its_address(self):
        parser = make_parser()
        result = parser._parse_interfaces(navbox())
        assert len(result) == 1
        assert result[0].name == "Vlan10"
        assert [a.address for a in result[0].addresses] == ["158.38.1.13/32"]
        assert "navsync" in result[0].tags

    def test_only_the_management_ip_should_be_primary(self):
        parser = make_parser()
        interfaces = [
            nav_interface(
                addresses=[
                    address(ip="158.38.1.13", prefix="158.38.1.0/24"),
                    address(ip="2001:700::1", prefix="2001:700::/64"),
                ]
            )
        ]
        result = parser._parse_interfaces(
            navbox(ip="158.38.1.13", interfaces=interfaces)
        )
        primary = {a.address: a.is_primary for a in result[0].addresses}
        assert primary == {"158.38.1.13/32": True, "2001:700::1/128": False}

    def test_an_ipv6_management_ip_should_be_the_primary_one(self):
        parser = make_parser()
        interfaces = [
            nav_interface(
                addresses=[
                    address(ip="158.38.1.13", prefix="158.38.1.0/24"),
                    address(ip="2001:700::1", prefix="2001:700::/64"),
                ]
            )
        ]
        result = parser._parse_interfaces(
            navbox(ip="2001:700::1", interfaces=interfaces)
        )
        primary = {a.address: a.is_primary for a in result[0].addresses}
        assert primary == {"158.38.1.13/32": False, "2001:700::1/128": True}

    def test_non_management_addresses_should_still_be_registered(self):
        """All addresses are synced, only the primary flag is selective"""
        parser = make_parser()
        interfaces = [
            nav_interface(name="Vlan10", addresses=[address(ip="158.38.1.13")]),
            nav_interface(name="Vlan20", addresses=[address(ip="158.38.2.13")]),
        ]
        result = parser._parse_interfaces(
            navbox(ip="158.38.1.13", interfaces=interfaces)
        )
        assert len(result) == 2
        assert sum(1 for i in result for a in i.addresses if a.is_primary) == 1, (
            "exactly one address is primary"
        )

    def test_navbox_without_management_ip_should_have_no_primary(self):
        parser = make_parser()
        result = parser._parse_interfaces(navbox(ip=""))
        assert len(result) == 1, "interfaces are still synced"
        assert not any(a.is_primary for a in result[0].addresses)

    def test_management_ip_not_on_any_interface_should_have_no_primary(self):
        parser = make_parser()
        result = parser._parse_interfaces(navbox(ip="158.38.99.99"))
        assert len(result) == 1
        assert not any(a.is_primary for a in result[0].addresses)

    def test_navbox_without_interfaces_should_produce_nothing(self):
        parser = make_parser()
        assert parser._parse_interfaces(navbox(interfaces=[])) == []

    def test_empty_description_should_become_none(self):
        parser = make_parser()
        result = parser._parse_interfaces(
            navbox(interfaces=[nav_interface(description="")])
        )
        assert result[0].description is None


def make_api_with_session(status_code=200, json_body=None, url_seen=None):
    """Builds an Api whose HTTP session returns a canned response"""
    api = Api.__new__(Api)
    api.base_url = "http://nav.example.org/api/1/"
    api.session = MagicMock()

    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    response.raise_for_status.side_effect = (
        requests.HTTPError(f"{status_code}") if status_code >= 400 else None
    )

    def session_get(url, params=None):
        if url_seen is not None:
            url_seen.append((url, params))
        return response

    api.session.get = session_get
    return api


class TestGetSingle:
    def test_detail_object_should_be_returned(self):
        api = make_api_with_session(json_body={"gw_ip": "158.38.1.13"})
        assert api.get_single("gwportprefix/10.0.0.1/") == {"gw_ip": "158.38.1.13"}

    def test_missing_object_should_return_none(self):
        api = make_api_with_session(status_code=404, json_body={"detail": "Not found."})
        assert api.get_single("gwportprefix/10.0.0.1/") is None

    def test_path_should_be_appended_to_the_base_url(self):
        url_seen = []
        api = make_api_with_session(json_body={}, url_seen=url_seen)
        api.get_single("/gwportprefix/10.0.0.1/")
        assert url_seen == [
            ("http://nav.example.org/api/1/gwportprefix/10.0.0.1/", None)
        ]

    def test_params_should_be_passed_on_to_the_request(self):
        url_seen = []
        api = make_api_with_session(json_body={}, url_seen=url_seen)
        api.get_single("gwportprefix/10.0.0.1/", params={"interface__netbox": 10})
        assert url_seen[0][1] == {"interface__netbox": 10}

    def test_list_response_should_raise_authentication_error(self):
        """A paginated response means we were served the API root, not the object"""
        api = make_api_with_session(json_body={"results": [], "next": None})
        with pytest.raises(AuthenticationError):
            api.get_single("gwportprefix/10.0.0.1/")

    def test_non_dict_response_should_raise_connection_error(self):
        api = make_api_with_session(json_body=[1, 2, 3])
        with pytest.raises(ConnectionError):
            api.get_single("gwportprefix/10.0.0.1/")

    def test_server_error_should_raise_connection_error(self):
        api = make_api_with_session(status_code=500, json_body={})
        with pytest.raises(ConnectionError):
            api.get_single("gwportprefix/10.0.0.1/")


class TestIsGloballyRoutable:
    def test_public_addresses_should_be_routable(self):
        assert EntityParser._is_globally_routable("158.38.1.13")
        assert EntityParser._is_globally_routable("2001:700::1")

    def test_rfc1918_addresses_should_not_be_routable(self):
        assert not EntityParser._is_globally_routable("10.130.12.1")
        assert not EntityParser._is_globally_routable("172.16.5.1")
        assert not EntityParser._is_globally_routable("192.168.1.1")

    def test_loopback_addresses_should_not_be_routable(self):
        assert not EntityParser._is_globally_routable("127.0.0.1")
        assert not EntityParser._is_globally_routable("::1")

    def test_link_local_addresses_should_not_be_routable(self):
        assert not EntityParser._is_globally_routable("169.254.1.1")
        assert not EntityParser._is_globally_routable("fe80::1")

    def test_ipv6_unique_local_addresses_should_not_be_routable(self):
        assert not EntityParser._is_globally_routable("fd00::1")

    def test_unparseable_addresses_should_not_be_routable(self):
        assert not EntityParser._is_globally_routable("not-an-address")
        assert not EntityParser._is_globally_routable("")


class TestParseInterfacesSkipsLocalAddresses:
    def test_private_addresses_should_not_be_parsed(self):
        parser = make_parser()
        interfaces = [nav_interface(addresses=[address(ip="10.130.12.1")])]
        result = parser._parse_interfaces(
            navbox(ip="158.38.1.13", interfaces=interfaces)
        )
        assert result == [], "an interface with only private addresses is skipped"

    def test_only_the_routable_addresses_of_an_interface_should_be_parsed(self):
        parser = make_parser()
        interfaces = [
            nav_interface(
                addresses=[
                    address(ip="158.38.1.13"),
                    address(ip="10.130.12.1"),
                    address(ip="2001:700::1", prefix="2001:700::/64"),
                ]
            )
        ]
        result = parser._parse_interfaces(
            navbox(ip="158.38.1.13", interfaces=interfaces)
        )
        assert [a.address for a in result[0].addresses] == [
            "158.38.1.13/32",
            "2001:700::1/128",
        ]

    def test_interfaces_with_only_private_addresses_should_be_dropped(self):
        parser = make_parser()
        interfaces = [
            nav_interface(name="Vlan10", addresses=[address(ip="158.38.1.13")]),
            nav_interface(name="Vlan20", addresses=[address(ip="192.168.1.1")]),
        ]
        result = parser._parse_interfaces(
            navbox(ip="158.38.1.13", interfaces=interfaces)
        )
        assert [i.name for i in result] == ["Vlan10"]

    def test_private_management_ip_should_not_become_primary(self):
        """A private management IP is filtered out like any other address"""
        parser = make_parser()
        interfaces = [
            nav_interface(
                addresses=[address(ip="10.130.12.1"), address(ip="158.38.1.13")]
            )
        ]
        result = parser._parse_interfaces(
            navbox(ip="10.130.12.1", interfaces=interfaces)
        )
        assert [a.address for a in result[0].addresses] == ["158.38.1.13/32"]
        assert not any(a.is_primary for a in result[0].addresses)
