from unittest.mock import MagicMock

from pynetbox.core.query import RequestError

from navsync import netbox as netbox_helpers
from navsync.parser import Device, Interface, IpAddress, Prefix
from navsync.syncer import Syncer


def make_syncer():
    syncer = Syncer.__new__(Syncer)
    syncer.netbox_api = MagicMock()
    syncer.tags = {"navsync": 1, "cnaas": 2}
    return syncer


def prefix(cidr="158.38.1.0/24"):
    return Prefix(prefix=cidr, tags=["navsync"])


def upstream_prefix(cidr="158.38.1.0/24"):
    record = MagicMock()
    record.prefix = cidr
    record.tags = []
    record.updates.return_value = {}
    return record


def device(name="gw-1", addresses=("158.38.1.13/32",), prefixes=("158.38.1.0/24",)):
    """Builds a Device with one interface holding the given addresses"""
    ip_addresses = [
        IpAddress(
            address=addr,
            tags=["navsync"],
            prefix=prefix(cidr) if cidr is not None else None,
        )
        for addr, cidr in zip(addresses, prefixes)
    ]
    return Device(
        name=name,
        tags=["navsync"],
        tenant=1,
        location=None,
        role="router",
        manufacturer="juniper",
        model="MX",
        nav_server="http://nav.example.org/",
        interfaces=[Interface(name="lo0", addresses=ip_addresses, tags=["navsync"])],
    )


class TestGetPrefixes:
    def test_prefixes_should_be_keyed_by_prefix(self):
        api = MagicMock()
        api.ipam.prefixes.all.return_value = [
            upstream_prefix("158.38.1.0/24"),
            upstream_prefix("2001:700::/64"),
        ]
        result = netbox_helpers.get_prefixes(api)
        assert sorted(result) == ["158.38.1.0/24", "2001:700::/64"]


class TestGetPrefixesToSync:
    def test_prefixes_should_be_deduplicated_across_devices(self):
        devices = {"gw-1": device(name="gw-1"), "gw-2": device(name="gw-2")}
        assert list(Syncer._get_prefixes_to_sync(devices)) == ["158.38.1.0/24"]

    def test_distinct_prefixes_should_all_be_returned(self):
        devices = {
            "gw-1": device(name="gw-1"),
            "gw-2": device(
                name="gw-2", addresses=("158.38.2.13/32",), prefixes=("158.38.2.0/24",)
            ),
        }
        result = Syncer._get_prefixes_to_sync(devices)
        assert sorted(result) == ["158.38.1.0/24", "158.38.2.0/24"]

    def test_addresses_without_a_prefix_should_be_ignored(self):
        devices = {"gw-1": device(addresses=("158.38.1.13/32",), prefixes=(None,))}
        assert Syncer._get_prefixes_to_sync(devices) == {}

    def test_devices_without_interfaces_should_contribute_nothing(self):
        no_interfaces = device()
        no_interfaces.interfaces = []
        assert Syncer._get_prefixes_to_sync({"gw-1": no_interfaces}) == {}


class TestSyncPrefix:
    def test_known_prefix_should_be_taken_from_the_prefetched_map(self):
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        existing = upstream_prefix()

        result = syncer._sync_prefix(prefix(), {"158.38.1.0/24": existing})

        assert result is existing
        syncer.netbox_api.ipam.prefixes.create.assert_not_called()
        syncer.netbox_api.ipam.prefixes.get.assert_not_called()

    def test_created_prefix_should_be_added_to_the_map(self):
        syncer = make_syncer()
        created = upstream_prefix()
        syncer.netbox_api.ipam.prefixes.create.return_value = created
        upstream_prefixes = {}

        result = syncer._sync_prefix(prefix(), upstream_prefixes)

        assert result is created
        assert upstream_prefixes["158.38.1.0/24"] is created

    def test_prefix_should_only_be_created_once_per_sync(self):
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        created = upstream_prefix()
        syncer.netbox_api.ipam.prefixes.create.return_value = created
        upstream_prefixes = {}

        syncer._sync_prefix(prefix(), upstream_prefixes)
        result = syncer._sync_prefix(prefix(), upstream_prefixes)

        assert result is created
        assert syncer.netbox_api.ipam.prefixes.create.call_count == 1

    def test_created_prefix_should_be_tagged_navsync_and_cnaas(self):
        syncer = make_syncer()
        syncer.netbox_api.ipam.prefixes.create.return_value = upstream_prefix()

        syncer._sync_prefix(prefix(), {})

        tags = syncer.netbox_api.ipam.prefixes.create.call_args.kwargs["tags"]
        assert sorted(tags) == [1, 2]

    def test_created_prefix_should_be_active(self):
        syncer = make_syncer()
        syncer.netbox_api.ipam.prefixes.create.return_value = upstream_prefix()

        syncer._sync_prefix(prefix(), {})

        kwargs = syncer.netbox_api.ipam.prefixes.create.call_args.kwargs
        assert kwargs["prefix"] == "158.38.1.0/24"
        assert kwargs["status"] == "active"

    def test_created_prefix_should_not_get_a_tenant_or_vlan(self):
        """NAV has no reliable tenant or VLAN for a prefix"""
        syncer = make_syncer()
        syncer.netbox_api.ipam.prefixes.create.return_value = upstream_prefix()

        syncer._sync_prefix(prefix(), {})

        kwargs = syncer.netbox_api.ipam.prefixes.create.call_args.kwargs
        assert "tenant" not in kwargs
        assert "vlan" not in kwargs

    def test_existing_prefix_should_only_have_its_tags_changed(self):
        """An existing prefix may be hand-curated, so nothing else is touched"""
        syncer = make_syncer()
        syncer._get_tag_ids_from_tags = lambda tags: []
        existing = upstream_prefix()
        existing.updates.return_value = {"tags": [1]}

        syncer._sync_prefix(prefix(), {"158.38.1.0/24": existing})

        assert existing.tags == [1]
        existing.save.assert_called_once()

    def test_failed_creation_should_not_be_added_to_the_map(self):
        syncer = make_syncer()
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"prefix": ["invalid"]}
        syncer.netbox_api.ipam.prefixes.create.side_effect = RequestError(response)
        upstream_prefixes = {}

        result = syncer._sync_prefix(prefix(), upstream_prefixes)

        assert result is None
        assert upstream_prefixes == {}


class TestSyncPrefixes:
    def test_no_prefixes_should_skip_the_upstream_fetch(self):
        syncer = make_syncer()
        devices = {"gw-1": device(addresses=("158.38.1.13/32",), prefixes=(None,))}

        syncer._sync_prefixes(devices)

        syncer.netbox_api.ipam.prefixes.all.assert_not_called()
        syncer.netbox_api.ipam.prefixes.create.assert_not_called()

    def test_each_distinct_prefix_should_be_created_once(self):
        syncer = make_syncer()
        syncer.netbox_api.ipam.prefixes.all.return_value = []
        syncer.netbox_api.ipam.prefixes.create.return_value = upstream_prefix()
        devices = {
            "gw-1": device(name="gw-1"),
            "gw-2": device(name="gw-2"),
            "gw-3": device(
                name="gw-3", addresses=("158.38.2.13/32",), prefixes=("158.38.2.0/24",)
            ),
        }

        syncer._sync_prefixes(devices)

        created = [
            call.kwargs["prefix"]
            for call in syncer.netbox_api.ipam.prefixes.create.call_args_list
        ]
        assert sorted(created) == ["158.38.1.0/24", "158.38.2.0/24"]

    def test_the_upstream_collection_should_only_be_fetched_once(self):
        syncer = make_syncer()
        syncer.netbox_api.ipam.prefixes.all.return_value = []
        syncer.netbox_api.ipam.prefixes.create.return_value = upstream_prefix()

        syncer._sync_prefixes(
            {"gw-1": device(name="gw-1"), "gw-2": device(name="gw-2")}
        )

        assert syncer.netbox_api.ipam.prefixes.all.call_count == 1
