from unittest.mock import MagicMock

from navsync.parser import Interface, IpAddress
from navsync.syncer import Syncer


def make_syncer():
    syncer = Syncer.__new__(Syncer)
    syncer.netbox_api = MagicMock()
    syncer.tags = {"navsync": 1, "cnaas": 2}
    return syncer


def upstream_interface(name="Vlan10", device_id=100):
    interface = MagicMock()
    interface.name = name
    interface.device = MagicMock(id=device_id)
    interface.tags = []
    interface.updates.return_value = {}
    return interface


def nav_interface(name="Vlan10", addresses=("10.0.0.1/24",)):
    return Interface(
        name=name,
        addresses=[nav_address(a) for a in addresses],
        tags=["navsync"],
    )


def nav_address(address="10.0.0.1/24", is_primary=False):
    return IpAddress(address=address, tags=["navsync"], is_primary=is_primary)


class TestSyncInterface:
    def test_known_interface_should_be_taken_from_the_prefetched_map(self):
        syncer = make_syncer()
        device = MagicMock(id=100, name="sw1")
        existing = upstream_interface()
        upstream_interfaces = {100: {"Vlan10": existing}}

        result = syncer._sync_interface(nav_interface(), device, upstream_interfaces)

        assert result is existing
        # no per-object request should be made when the map already has it
        syncer.netbox_api.dcim.interfaces.get.assert_not_called()
        syncer.netbox_api.dcim.interfaces.create.assert_not_called()

    def test_interface_on_another_device_should_not_be_reused(self):
        syncer = make_syncer()
        device = MagicMock(id=100, name="sw1")
        created = upstream_interface()
        syncer.netbox_api.dcim.interfaces.create.return_value = created
        # same interface name, but on a different device
        upstream_interfaces = {200: {"Vlan10": upstream_interface(device_id=200)}}

        result = syncer._sync_interface(nav_interface(), device, upstream_interfaces)

        assert result is created
        syncer.netbox_api.dcim.interfaces.create.assert_called_once()

    def test_created_interface_should_be_added_to_the_map(self):
        syncer = make_syncer()
        device = MagicMock(id=100, name="sw1")
        created = upstream_interface()
        syncer.netbox_api.dcim.interfaces.create.return_value = created
        upstream_interfaces = {}

        syncer._sync_interface(nav_interface(), device, upstream_interfaces)

        assert upstream_interfaces[100]["Vlan10"] is created

    def test_interface_should_only_be_created_once_per_sync(self):
        syncer = make_syncer()
        device = MagicMock(id=100, name="sw1")
        created = upstream_interface()
        syncer.netbox_api.dcim.interfaces.create.return_value = created
        upstream_interfaces = {}

        syncer._sync_interface(nav_interface(), device, upstream_interfaces)
        result = syncer._sync_interface(nav_interface(), device, upstream_interfaces)

        assert result is created
        assert syncer.netbox_api.dcim.interfaces.create.call_count == 1

    def test_failed_creation_should_not_be_added_to_the_map(self):
        from pynetbox.core.query import RequestError

        syncer = make_syncer()
        device = MagicMock(id=100, name="sw1")
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"name": ["invalid"]}
        syncer.netbox_api.dcim.interfaces.create.side_effect = RequestError(response)
        upstream_interfaces = {}

        result = syncer._sync_interface(nav_interface(), device, upstream_interfaces)

        assert result is None
        assert upstream_interfaces == {}


class TestSyncIpAddress:
    def test_created_ip_address_should_be_added_to_the_map(self):
        syncer = make_syncer()
        created = MagicMock()
        created.address = "10.0.0.1/24"
        syncer.netbox_api.ipam.ip_addresses.create.return_value = created
        upstream_ip_addresses = {}

        result = syncer._sync_ip_address(
            nav_address(), upstream_interface(), upstream_ip_addresses
        )

        assert result is created
        assert upstream_ip_addresses["10.0.0.1"] is created
        syncer.netbox_api.ipam.ip_addresses.get.assert_not_called()

    def test_ip_address_should_only_be_created_once_per_sync(self):
        syncer = make_syncer()
        created = MagicMock()
        created.address = "10.0.0.1/24"
        created.assigned_object_type = "dcim.interface"
        created.tags = []
        created.updates.return_value = {}
        syncer.netbox_api.ipam.ip_addresses.create.return_value = created
        upstream_ip_addresses = {}
        interface = upstream_interface()
        created.assigned_object_id = interface.id

        syncer._sync_ip_address(nav_address(), interface, upstream_ip_addresses)
        syncer._sync_ip_address(nav_address(), interface, upstream_ip_addresses)

        assert syncer.netbox_api.ipam.ip_addresses.create.call_count == 1

    def test_known_ip_address_should_be_taken_from_the_prefetched_map(self):
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        interface = upstream_interface()
        existing = MagicMock()
        existing.address = "10.0.0.1/24"
        existing.assigned_object_type = "dcim.interface"
        existing.assigned_object_id = interface.id
        existing.tags = []
        existing.updates.return_value = {}

        result = syncer._sync_ip_address(
            nav_address(), interface, {"10.0.0.1": existing}
        )

        assert result is existing
        syncer.netbox_api.ipam.ip_addresses.get.assert_not_called()
        syncer.netbox_api.ipam.ip_addresses.create.assert_not_called()

    def test_address_registered_with_another_mask_should_keep_that_mask(self):
        """
        Netbox treats '158.38.1.13/31' as a duplicate of an existing
        '158.38.1.13/32', so the existing record is found rather than created
        again. Its mask may have been set deliberately, so it is left alone.
        """
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        interface = upstream_interface()
        existing = MagicMock()
        existing.address = "158.38.1.13/31"
        existing.assigned_object_type = "dcim.interface"
        existing.assigned_object_id = interface.id
        existing.tags = []
        existing.updates.return_value = {}

        result = syncer._sync_ip_address(
            nav_address("158.38.1.13/32"), interface, {"158.38.1.13": existing}
        )

        assert result is existing
        assert existing.address == "158.38.1.13/31", "the existing mask is kept"
        syncer.netbox_api.ipam.ip_addresses.create.assert_not_called()
        existing.save.assert_not_called()

    def test_unparseable_address_should_not_be_synced(self):
        syncer = make_syncer()

        result = syncer._sync_ip_address(
            nav_address("not-an-address"), upstream_interface(), {}
        )

        assert result is None
        syncer.netbox_api.ipam.ip_addresses.create.assert_not_called()


class TestSetPrimaryIpAddress:
    def test_ipv4_address_should_become_primary_ip4(self):
        syncer = make_syncer()
        device = MagicMock(name="sw1")
        device.primary_ip4 = None
        device.primary_ip6 = None
        ip_address = MagicMock(id=55)
        ip_address.address = "10.0.0.1/32"

        syncer._set_primary_ip_address(device, ip_address)

        assert device.primary_ip4 == 55
        assert device.primary_ip6 is None
        device.save.assert_called_once()

    def test_ipv6_address_should_become_primary_ip6(self):
        syncer = make_syncer()
        device = MagicMock(name="sw1")
        device.primary_ip4 = None
        device.primary_ip6 = None
        ip_address = MagicMock(id=66)
        ip_address.address = "2001:db8::1/128"

        syncer._set_primary_ip_address(device, ip_address)

        assert device.primary_ip6 == 66
        assert device.primary_ip4 is None

    def test_already_primary_address_should_not_be_saved_again(self):
        syncer = make_syncer()
        device = MagicMock(name="sw1")
        ip_address = MagicMock(id=55)
        ip_address.address = "10.0.0.1/32"
        device.primary_ip4 = MagicMock(id=55)

        syncer._set_primary_ip_address(device, ip_address)

        device.save.assert_not_called()

    def test_a_different_primary_address_should_be_replaced(self):
        syncer = make_syncer()
        device = MagicMock(name="sw1")
        device.primary_ip4 = MagicMock(id=99)
        ip_address = MagicMock(id=55)
        ip_address.address = "10.0.0.1/32"

        syncer._set_primary_ip_address(device, ip_address)

        assert device.primary_ip4 == 55
        device.save.assert_called_once()

    def test_unparseable_address_should_not_be_set_as_primary(self):
        syncer = make_syncer()
        device = MagicMock(name="sw1")
        device.primary_ip4 = None
        ip_address = MagicMock(id=55)
        ip_address.address = "not-an-address"

        syncer._set_primary_ip_address(device, ip_address)

        assert device.primary_ip4 is None
        device.save.assert_not_called()

    def test_failed_save_should_not_raise(self):
        from pynetbox.core.query import RequestError

        syncer = make_syncer()
        device = MagicMock(name="sw1")
        device.primary_ip4 = None
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"primary_ip4": ["invalid"]}
        device.save.side_effect = RequestError(response)
        ip_address = MagicMock(id=55)
        ip_address.address = "10.0.0.1/32"

        syncer._set_primary_ip_address(device, ip_address)

        device.save.assert_called_once()


class TestReleasePrimaryIpAddress:
    def test_the_holding_device_should_have_its_primary_ip_cleared(self):
        syncer = make_syncer()
        ip_address = MagicMock(id=55)
        ip_address.address = "158.38.1.13/32"
        holder = MagicMock()
        holder.name = "gw-old"
        holder.tags = []
        syncer.netbox_api.dcim.devices.get.return_value = holder

        syncer._release_primary_ip_address(ip_address)

        assert holder.primary_ip4 is None
        holder.save.assert_called_once()

    def test_the_holding_device_should_be_tagged_navsync(self):
        """navsync has modified the device, so it marks it as its own"""
        syncer = make_syncer()
        ip_address = MagicMock(id=55)
        ip_address.address = "158.38.1.13/32"
        holder = MagicMock()
        holder.name = "gw-old"
        holder.tags = []
        syncer.netbox_api.dcim.devices.get.return_value = holder

        syncer._release_primary_ip_address(ip_address)

        assert holder.tags == [syncer.tags["navsync"]]

    def test_existing_tags_on_the_holding_device_should_be_kept(self):
        syncer = make_syncer()
        ip_address = MagicMock(id=55)
        ip_address.address = "158.38.1.13/32"
        holder = MagicMock()
        holder.name = "gw-old"
        holder.tags = [MagicMock(id=7)]
        syncer.netbox_api.dcim.devices.get.return_value = holder

        syncer._release_primary_ip_address(ip_address)

        assert holder.tags == [syncer.tags["navsync"], 7]

    def test_only_the_matching_address_family_should_be_queried(self):
        """An IPv4 address can only ever be in primary_ip4"""
        syncer = make_syncer()
        syncer.netbox_api.dcim.devices.get.return_value = None
        ip_address = MagicMock(id=55)
        ip_address.address = "158.38.1.13/32"

        syncer._release_primary_ip_address(ip_address)

        assert syncer.netbox_api.dcim.devices.get.call_args.kwargs == {
            "primary_ip4_id": 55
        }

    def test_an_ipv6_address_should_only_query_primary_ip6(self):
        syncer = make_syncer()
        syncer.netbox_api.dcim.devices.get.return_value = None
        ip_address = MagicMock(id=66)
        ip_address.address = "2001:700::1/128"

        syncer._release_primary_ip_address(ip_address)

        assert syncer.netbox_api.dcim.devices.get.call_args.kwargs == {
            "primary_ip6_id": 66
        }

    def test_an_unparseable_address_should_not_be_queried_at_all(self):
        syncer = make_syncer()
        ip_address = MagicMock(id=55)
        ip_address.address = "not-an-address"

        syncer._release_primary_ip_address(ip_address)

        syncer.netbox_api.dcim.devices.get.assert_not_called()

    def test_nothing_should_be_saved_when_no_device_holds_the_address(self):
        syncer = make_syncer()
        syncer.netbox_api.dcim.devices.get.return_value = None
        ip_address = MagicMock(id=55)
        ip_address.address = "158.38.1.13/32"

        syncer._release_primary_ip_address(ip_address)

        syncer.netbox_api.dcim.devices.get.assert_called_once()

    def test_a_failed_clear_should_not_raise(self):
        from pynetbox.core.query import RequestError

        syncer = make_syncer()
        ip_address = MagicMock(id=55)
        ip_address.address = "158.38.1.13/32"
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"primary_ip4": ["nope"]}
        holder = MagicMock()
        holder.name = "gw-old"
        holder.tags = []
        holder.save.side_effect = RequestError(response)
        syncer.netbox_api.dcim.devices.get.return_value = holder

        syncer._release_primary_ip_address(ip_address)

        holder.save.assert_called_once()

    def test_reassignment_should_release_the_primary_ip_first(self):
        """Netbox refuses to reassign an address that is still a primary IP"""
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        interface = upstream_interface()
        existing = MagicMock()
        existing.address = "158.38.1.13/32"
        existing.assigned_object_type = "dcim.interface"
        existing.assigned_object_id = 999  # on some other interface
        existing.tags = []
        existing.updates.return_value = {"assigned_object_id": interface.id}
        released = []
        syncer._release_primary_ip_address = lambda ip: released.append(ip)

        syncer._sync_ip_address(
            nav_address("158.38.1.13/32"), interface, {"158.38.1.13": existing}
        )

        assert released == [existing], "released before being reassigned"
        assert existing.assigned_object_id == interface.id

    def test_an_already_assigned_address_should_not_be_released(self):
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        interface = upstream_interface()
        existing = MagicMock()
        existing.address = "158.38.1.13/32"
        existing.assigned_object_type = "dcim.interface"
        existing.assigned_object_id = interface.id
        existing.tags = []
        existing.updates.return_value = {}
        released = []
        syncer._release_primary_ip_address = lambda ip: released.append(ip)

        syncer._sync_ip_address(
            nav_address("158.38.1.13/32"), interface, {"158.38.1.13": existing}
        )

        assert released == [], "no need to release an address already in place"
