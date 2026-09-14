from unittest.mock import MagicMock

from pynetbox.core.query import RequestError

from navsync.syncer import Syncer


def make_syncer():
    syncer = Syncer.__new__(Syncer)
    syncer.netbox_api = MagicMock()
    syncer.tags = {"navsync": 1, "cnaas": 2}
    return syncer


def upstream_ip_address(address="158.38.1.13/32", status="Active", assigned_to=7):
    record = MagicMock()
    record.address = address
    record.status = status
    record.assigned_object_id = assigned_to
    return record


def upstream_prefix(cidr="158.38.1.0/24", status="Active"):
    record = MagicMock()
    record.prefix = cidr
    record.status = status
    return record


class TestDeprecateUnassignedIpAddresses:
    def test_an_unassigned_address_should_be_deprecated(self):
        syncer = make_syncer()
        ip = upstream_ip_address(assigned_to=None)
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._deprecate_unassigned_ip_addresses()

        assert ip.status == "deprecated"
        ip.save.assert_called_once()

    def test_an_assigned_address_should_be_left_alone(self):
        """Someone may have put the address back into use themselves"""
        syncer = make_syncer()
        ip = upstream_ip_address(assigned_to=7)
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._deprecate_unassigned_ip_addresses()

        assert ip.status == "Active"
        ip.save.assert_not_called()

    def test_an_already_deprecated_address_should_not_be_saved_again(self):
        syncer = make_syncer()
        ip = upstream_ip_address(status="Deprecated", assigned_to=None)
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._deprecate_unassigned_ip_addresses()

        ip.save.assert_not_called()

    def test_only_addresses_navsync_owns_should_be_considered(self):
        syncer = make_syncer()
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = []

        syncer._deprecate_unassigned_ip_addresses()

        assert syncer.netbox_api.ipam.ip_addresses.filter.call_args.kwargs == {
            "tag": ["navsync", "cnaas"]
        }

    def test_a_failed_save_should_not_raise(self):
        syncer = make_syncer()
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"status": ["nope"]}
        ip = upstream_ip_address(assigned_to=None)
        ip.save.side_effect = RequestError(response)
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._deprecate_unassigned_ip_addresses()

        ip.save.assert_called_once()


class TestDeprecateEmptyPrefixes:
    def test_an_empty_prefix_should_be_deprecated(self):
        syncer = make_syncer()
        prefix = upstream_prefix()
        syncer.netbox_api.ipam.prefixes.filter.return_value = [prefix]
        syncer.netbox_api.ipam.ip_addresses.count.return_value = 0

        syncer._deprecate_empty_prefixes()

        assert prefix.status == "deprecated"
        prefix.save.assert_called_once()

    def test_a_prefix_still_holding_addresses_should_stay_active(self):
        """Addresses registered elsewhere keep the prefix in use"""
        syncer = make_syncer()
        prefix = upstream_prefix()
        syncer.netbox_api.ipam.prefixes.filter.return_value = [prefix]
        syncer.netbox_api.ipam.ip_addresses.count.return_value = 3

        syncer._deprecate_empty_prefixes()

        assert prefix.status == "Active"
        prefix.save.assert_not_called()

    def test_deprecated_addresses_should_not_keep_a_prefix_active(self):
        syncer = make_syncer()
        prefix = upstream_prefix()
        syncer.netbox_api.ipam.prefixes.filter.return_value = [prefix]
        syncer.netbox_api.ipam.ip_addresses.count.return_value = 0

        syncer._deprecate_empty_prefixes()

        assert syncer.netbox_api.ipam.ip_addresses.count.call_args.kwargs == {
            "parent": "158.38.1.0/24",
            "status__n": "deprecated",
        }

    def test_an_already_deprecated_prefix_should_not_be_saved_again(self):
        syncer = make_syncer()
        prefix = upstream_prefix(status="Deprecated")
        syncer.netbox_api.ipam.prefixes.filter.return_value = [prefix]

        syncer._deprecate_empty_prefixes()

        prefix.save.assert_not_called()

    def test_only_prefixes_navsync_owns_should_be_considered(self):
        syncer = make_syncer()
        syncer.netbox_api.ipam.prefixes.filter.return_value = []

        syncer._deprecate_empty_prefixes()

        assert syncer.netbox_api.ipam.prefixes.filter.call_args.kwargs == {
            "tag": ["navsync", "cnaas"]
        }


class TestUnassignIpAddressesFromDevice:
    def test_every_address_on_the_device_should_be_unassigned(self):
        syncer = make_syncer()
        syncer._release_primary_ip_address = lambda ip: None
        device = MagicMock(id=100)
        device.name = "gw-old"
        ip = upstream_ip_address()
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._unassign_ip_addresses_from_device(device)

        assert ip.assigned_object_id is None
        assert ip.assigned_object_type is None
        ip.save.assert_called_once()

    def test_the_addresses_should_be_looked_up_by_device(self):
        syncer = make_syncer()
        device = MagicMock(id=100)
        device.name = "gw-old"
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = []

        syncer._unassign_ip_addresses_from_device(device)

        assert syncer.netbox_api.ipam.ip_addresses.filter.call_args.kwargs == {
            "device_id": 100
        }

    def test_the_primary_ip_should_be_released_first(self):
        """Netbox refuses to unassign an address that is still a primary IP"""
        syncer = make_syncer()
        released = []
        syncer._release_primary_ip_address = lambda ip: released.append(ip)
        device = MagicMock(id=100)
        device.name = "gw-old"
        ip = upstream_ip_address()
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._unassign_ip_addresses_from_device(device)

        assert released == [ip]

    def test_a_failed_save_should_not_raise(self):
        syncer = make_syncer()
        syncer._release_primary_ip_address = lambda ip: None
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"assigned_object_id": ["nope"]}
        device = MagicMock(id=100)
        device.name = "gw-old"
        ip = upstream_ip_address()
        ip.save.side_effect = RequestError(response)
        syncer.netbox_api.ipam.ip_addresses.filter.return_value = [ip]

        syncer._unassign_ip_addresses_from_device(device)

        ip.save.assert_called_once()


class TestReactivation:
    def test_a_deprecated_address_in_use_again_should_become_active(self):
        from navsync.parser import IpAddress

        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        syncer._release_primary_ip_address = lambda ip: None
        interface = MagicMock(id=7)
        interface.name = "lo0"
        interface.device.name = "gw-1"
        existing = MagicMock()
        existing.address = "158.38.1.13/32"
        existing.status = "Deprecated"
        existing.assigned_object_type = "dcim.interface"
        existing.assigned_object_id = 7
        existing.tags = []
        existing.updates.return_value = {"status": "active"}

        syncer._sync_ip_address(
            IpAddress(address="158.38.1.13/32", tags=["navsync"]),
            interface,
            {"158.38.1.13": existing},
        )

        assert existing.status == "active"

    def test_an_active_address_should_keep_its_status(self):
        from navsync.parser import IpAddress

        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        interface = MagicMock(id=7)
        interface.name = "lo0"
        interface.device.name = "gw-1"
        existing = MagicMock()
        existing.address = "158.38.1.13/32"
        existing.status = "Active"
        existing.assigned_object_type = "dcim.interface"
        existing.assigned_object_id = 7
        existing.tags = []
        existing.updates.return_value = {}

        syncer._sync_ip_address(
            IpAddress(address="158.38.1.13/32", tags=["navsync"]),
            interface,
            {"158.38.1.13": existing},
        )

        assert existing.status == "Active"

    def test_a_deprecated_prefix_in_use_again_should_become_active(self):
        from navsync.parser import Prefix

        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        existing = upstream_prefix(status="Deprecated")
        existing.tags = []
        existing.updates.return_value = {"status": "active"}

        syncer._sync_prefix(
            Prefix(prefix="158.38.1.0/24", tags=["navsync"]),
            {"158.38.1.0/24": existing},
        )

        assert existing.status == "active"
