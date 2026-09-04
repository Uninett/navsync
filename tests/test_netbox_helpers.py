from unittest.mock import MagicMock

from navsync import netbox as netbox_helpers


def upstream_interface(name="Vlan10", device_id=100):
    interface = MagicMock()
    interface.name = name
    interface.device = MagicMock(id=device_id)
    return interface


def upstream_ip_address(address="10.0.0.1/24"):
    ip_address = MagicMock()
    ip_address.address = address
    return ip_address


class TestGetInterfaces:
    def test_interfaces_should_be_keyed_by_device_id_and_name(self):
        api = MagicMock()
        api.dcim.interfaces.all.return_value = [
            upstream_interface("Vlan10", device_id=100),
            upstream_interface("Vlan20", device_id=100),
            upstream_interface("Vlan10", device_id=200),
        ]
        result = netbox_helpers.get_interfaces(api)
        assert sorted(result[100]) == ["Vlan10", "Vlan20"]
        assert sorted(result[200]) == ["Vlan10"]

    def test_interfaces_without_a_device_should_be_skipped(self):
        api = MagicMock()
        orphan = upstream_interface()
        orphan.device = None
        api.dcim.interfaces.all.return_value = [orphan]
        assert netbox_helpers.get_interfaces(api) == {}


class TestGetIpAddresses:
    def test_ip_addresses_should_be_keyed_by_host_address(self):
        """Netbox enforces uniqueness on the host address, not on the mask"""
        api = MagicMock()
        ip_address = upstream_ip_address("10.0.0.1/24")
        api.ipam.ip_addresses.all.return_value = [ip_address]
        assert netbox_helpers.get_ip_addresses(api) == {"10.0.0.1": ip_address}

    def test_ipv6_addresses_should_be_keyed_by_host_address(self):
        api = MagicMock()
        ip_address = upstream_ip_address("2001:db8::1/64")
        api.ipam.ip_addresses.all.return_value = [ip_address]
        assert netbox_helpers.get_ip_addresses(api) == {"2001:db8::1": ip_address}

    def test_unparseable_addresses_should_be_skipped(self):
        api = MagicMock()
        api.ipam.ip_addresses.all.return_value = [upstream_ip_address("not-an-address")]
        assert netbox_helpers.get_ip_addresses(api) == {}


class TestHostAddress:
    def test_mask_should_be_stripped(self):
        assert netbox_helpers.host_address("10.0.0.1/24") == "10.0.0.1"

    def test_ipv6_mask_should_be_stripped(self):
        assert netbox_helpers.host_address("2001:db8::1/64") == "2001:db8::1"

    def test_address_without_a_mask_should_be_returned_as_is(self):
        assert netbox_helpers.host_address("10.0.0.1") == "10.0.0.1"

    def test_unparseable_address_should_return_none(self):
        assert netbox_helpers.host_address("not-an-address") is None
