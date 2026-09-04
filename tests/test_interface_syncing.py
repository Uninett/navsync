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
