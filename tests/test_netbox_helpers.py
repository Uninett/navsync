from unittest.mock import MagicMock

import pytest
from pynetbox.core.query import RequestError

from navsync import netbox as netbox_helpers
from navsync.netbox import UpstreamDevices


def upstream_interface(name="Vlan10", device_id=100):
    interface = MagicMock()
    interface.name = name
    interface.device = MagicMock(id=device_id)
    return interface


def upstream_ip_address(address="10.0.0.1/24"):
    ip_address = MagicMock()
    ip_address.address = address
    return ip_address


class TestGetDevices:
    def test_devices_should_be_keyed_by_name(self):
        api = MagicMock()
        device = MagicMock()
        device.name = "sw1"
        api.dcim.devices.filter.return_value = [device]
        assert netbox_helpers.get_devices(api) == {"sw1": device}

    def test_config_context_should_not_be_fetched(self):
        api = MagicMock()
        api.dcim.devices.filter.return_value = []
        netbox_helpers.get_devices(api)
        api.dcim.devices.filter.assert_called_once_with(exclude="config_context")


def upstream_device(name="sw1", primary_ip4=None, primary_ip6=None):
    device = MagicMock()
    device.name = name
    device.primary_ip4 = MagicMock(id=primary_ip4) if primary_ip4 else None
    device.primary_ip6 = MagicMock(id=primary_ip6) if primary_ip6 else None
    return device


def request_error():
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"name": ["invalid"]}
    return RequestError(response)


class TestUpstreamDevices:
    def test_devices_should_be_fetched_once_and_keyed_by_name(self):
        api = MagicMock()
        device = upstream_device("sw1")
        api.dcim.devices.filter.return_value = [device]
        devices = UpstreamDevices.fetch(api)
        assert devices.get("sw1") is device
        assert devices.values() == [device]
        api.dcim.devices.filter.assert_called_once()

    def test_unknown_device_should_be_none(self):
        assert UpstreamDevices({}).get("sw1") is None

    def test_added_device_should_be_found(self):
        devices = UpstreamDevices({})
        device = upstream_device("sw1")
        devices.add(device)
        assert devices.get("sw1") is device
        assert devices.values() == [device]

    def test_device_holding_a_primary_ip_should_be_found(self):
        holder = upstream_device("gw1", primary_ip4=55)
        devices = UpstreamDevices({"sw1": upstream_device("sw1"), "gw1": holder})
        assert devices.find_by_primary_ip("primary_ip4", 55) is holder

    def test_primary_ip_should_only_be_looked_for_in_the_given_field(self):
        holder = upstream_device("gw1", primary_ip6=55)
        devices = UpstreamDevices({"gw1": holder})
        assert devices.find_by_primary_ip("primary_ip4", 55) is None
        assert devices.find_by_primary_ip("primary_ip6", 55) is holder

    def test_saved_device_should_not_be_reloaded(self):
        """pynetbox updates the record from the response to a save itself"""
        device = upstream_device()
        device.save.return_value = True
        assert UpstreamDevices.save(device) is True
        device.full_details.assert_not_called()

    def test_device_should_be_reloaded_when_its_save_is_rejected(self):
        device = upstream_device()
        device.save.side_effect = request_error()
        with pytest.raises(RequestError):
            UpstreamDevices.save(device)
        device.full_details.assert_called_once()


class TestGetNavServers:
    def test_config_context_should_not_be_fetched(self):
        api = MagicMock()
        api.virtualization.virtual_machines.filter.return_value = []
        api.dcim.devices.filter.return_value = []
        list(netbox_helpers.get_nav_servers(api, https=True))
        vm_filters = api.virtualization.virtual_machines.filter.call_args.kwargs
        device_filters = api.dcim.devices.filter.call_args.kwargs
        assert vm_filters["exclude"] == "config_context"
        assert device_filters["exclude"] == "config_context"


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
