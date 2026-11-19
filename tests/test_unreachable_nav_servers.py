from datetime import timedelta
from unittest.mock import MagicMock, patch

from pynetbox.core.response import Record

from navsync.nav import ConnectionError
from navsync.netbox import UpstreamDevices
from navsync.parser import Device, Location, NavFetcher, Site
from navsync.syncer import Syncer
from navsync.utils import NavServerInfo

UNREACHABLE = "https://vk-down.example.org/"
REACHABLE = "https://vk-up.example.org/"


def make_syncer():
    syncer = Syncer.__new__(Syncer)
    syncer.netbox_api = MagicMock()
    syncer.tags = {"navsync": 1, "cnaas": 2}
    syncer.unreachable_nav_servers = {UNREACHABLE}
    syncer.upstream_devices = UpstreamDevices({})
    return syncer


def tags(*names):
    result = []
    for name in names:
        tag = MagicMock()
        tag.name = name
        result.append(tag)
    return result


def upstream_record(nav_server, status="active", **attributes):
    record = MagicMock(**attributes)
    record.tags = tags("navsync", "cnaas")
    record.status.value = status
    record.custom_fields = {"nav_server": nav_server} if nav_server else {}
    return record


class TestDecommissioning:
    def test_a_device_from_an_unreachable_nav_server_should_be_left_alone(self):
        syncer = make_syncer()
        device = upstream_record(UNREACHABLE)

        syncer._decommission_all_missing_devices({}, [device])

        device.save.assert_not_called()

    def test_a_device_from_a_reachable_nav_server_should_be_decommissioned(self):
        syncer = make_syncer()
        syncer._unassign_ip_addresses_from_device = lambda device: None
        device = upstream_record(REACHABLE)

        syncer._decommission_all_missing_devices({}, [device])

        assert device.status == "inventory"
        device.save.assert_called_once()

    def test_a_device_without_a_nav_server_should_be_decommissioned(self):
        syncer = make_syncer()
        syncer._unassign_ip_addresses_from_device = lambda device: None
        device = upstream_record(None)

        syncer._decommission_all_missing_devices({}, [device])

        assert device.status == "inventory"
        device.save.assert_called_once()

    def test_an_asset_from_an_unreachable_nav_server_should_be_left_alone(self):
        syncer = make_syncer()
        asset = upstream_record(UNREACHABLE)
        asset.status = "used"

        syncer._shelve_all_missing_assets({1: {"SERIAL": asset}}, [])

        asset.save.assert_not_called()

    def test_an_asset_from_a_reachable_nav_server_should_be_shelved(self):
        syncer = make_syncer()
        asset = upstream_record(REACHABLE)
        asset.status = "used"

        syncer._shelve_all_missing_assets({1: {"SERIAL": asset}}, [])

        assert asset.status == "stored"
        asset.save.assert_called_once()

    def test_a_virtual_chassis_from_an_unreachable_nav_server_should_be_kept(self):
        syncer = make_syncer()
        syncer._unregister_devices_as_members_of_vc = MagicMock()
        chassis = upstream_record(UNREACHABLE)

        syncer._delete_all_missing_virtual_chassis({}, {"vc": chassis}, [])

        chassis.delete.assert_not_called()

    def test_a_virtual_chassis_from_a_reachable_nav_server_should_be_deleted(self):
        syncer = make_syncer()
        syncer._unregister_devices_as_members_of_vc = MagicMock()
        chassis = upstream_record(REACHABLE)

        syncer._delete_all_missing_virtual_chassis({}, {"vc": chassis}, [])

        chassis.delete.assert_called_once()

    def test_a_site_from_an_unreachable_nav_server_should_be_left_alone(self):
        syncer = make_syncer()
        site = upstream_record(UNREACHABLE, id=1)

        syncer._decommission_all_missing_sites([site], set())

        site.save.assert_not_called()

    def test_a_site_from_a_reachable_nav_server_should_be_retired(self):
        syncer = make_syncer()
        site = upstream_record(REACHABLE, id=1)

        syncer._decommission_all_missing_sites([site], set())

        assert site.status == "retired"
        site.save.assert_called_once()

    def test_a_location_from_an_unreachable_nav_server_should_be_left_alone(self):
        syncer = make_syncer()
        location = upstream_record(UNREACHABLE, id=1)

        syncer._decommission_all_missing_locations(
            {"site": {"loc": {None: location}}}, set()
        )

        location.save.assert_not_called()

    def test_a_location_from_a_reachable_nav_server_should_be_retired(self):
        syncer = make_syncer()
        location = upstream_record(REACHABLE, id=1)

        syncer._decommission_all_missing_locations(
            {"site": {"loc": {None: location}}}, set()
        )

        assert location.status == "retired"
        location.save.assert_called_once()


def make_site():
    return Site(
        name="site",
        tags=["navsync"],
        slug="site",
        tenant=1,
        locations={},
        nav_server=REACHABLE,
    )


def make_location(site):
    return Location(
        name="room",
        tags=["navsync"],
        slug="room",
        tenant=1,
        site=site,
        nav_server=REACHABLE,
    )


def make_device():
    site = make_site()
    return Device(
        name="sw1",
        tags=["navsync"],
        tenant=1,
        location=make_location(site),
        role="switch",
        manufacturer="cisco",
        model="C9300",
        nav_server=REACHABLE,
    )


class TestRecordingNavServer:
    def test_a_created_site_should_record_its_nav_server(self):
        syncer = make_syncer()

        syncer._create_site(make_site())

        kwargs = syncer.netbox_api.dcim.sites.create.call_args.kwargs
        assert kwargs["custom_fields"]["nav_server"] == REACHABLE

    def test_an_updated_site_should_record_its_nav_server(self):
        syncer = make_syncer()
        upstream_site = MagicMock(tags=[], custom_fields={})

        syncer._update_site(make_site(), upstream_site)

        assert upstream_site.custom_fields["nav_server"] == REACHABLE

    def test_a_created_location_should_record_its_nav_server(self):
        syncer = make_syncer()
        site = make_site()

        syncer._create_location(make_location(site), MagicMock(id=1))

        kwargs = syncer.netbox_api.dcim.locations.create.call_args.kwargs
        assert kwargs["custom_fields"]["nav_server"] == REACHABLE

    def test_an_updated_location_should_record_its_nav_server(self):
        syncer = make_syncer()
        upstream_location = MagicMock(tags=[], custom_fields={})

        syncer._update_location(make_location(make_site()), upstream_location)

        assert upstream_location.custom_fields["nav_server"] == REACHABLE

    def test_a_created_device_should_record_its_nav_server(self):
        syncer = make_syncer()
        syncer.get_or_create_device_type = MagicMock(return_value=MagicMock(id=3))

        syncer._create_device(make_device(), MagicMock(id=1), MagicMock(id=2))

        kwargs = syncer.netbox_api.dcim.devices.create.call_args.kwargs
        assert kwargs["custom_fields"]["nav_server"] == REACHABLE

    def test_a_created_device_should_be_available_to_later_steps(self):
        syncer = make_syncer()
        syncer.get_or_create_device_type = MagicMock(return_value=MagicMock(id=3))
        created = MagicMock()
        created.name = "sw1"
        syncer.netbox_api.dcim.devices.create.return_value = created

        syncer._create_device(make_device(), MagicMock(id=1), MagicMock(id=2))

        assert syncer.upstream_devices.get("sw1") is created

    def test_an_updated_device_should_record_its_nav_server(self):
        syncer = make_syncer()
        syncer.get_or_create_device_type = MagicMock(return_value=MagicMock(id=3))
        upstream_device = MagicMock(tags=[], custom_fields={})

        syncer._update_device(
            make_device(), upstream_device, MagicMock(id=1), MagicMock(id=2)
        )

        assert upstream_device.custom_fields["nav_server"] == REACHABLE


def netbox_device(**overrides):
    """A real pynetbox record for a device that already matches make_device()"""
    values = {
        "id": 10,
        "name": "sw1",
        "device_type": {"id": 3, "model": "C9300"},
        "role": {"id": 2, "slug": "switch"},
        "site": {"id": 1, "slug": "site"},
        "location": {"id": 4, "name": "room"},
        "tenant": {"id": 1, "name": "tenant"},
        "status": {"value": "active", "label": "Active"},
        "tags": [{"id": 1, "name": "navsync"}],
        "custom_fields": {"nav_server": REACHABLE},
        **overrides,
    }
    return Record(values, MagicMock(), MagicMock())


class TestUpdatingDevices:
    def update(self, upstream_device, device_type=None):
        syncer = make_syncer()
        device = make_device()
        device.status = "active"
        syncer.get_or_create_device_type = MagicMock(
            return_value=device_type or upstream_device.device_type
        )
        syncer.upstream_devices.save = MagicMock()
        syncer._update_device(
            device,
            upstream_device,
            upstream_device.site,
            upstream_device.role,
            upstream_device.location,
        )

    def test_an_unchanged_device_should_have_nothing_to_save(self):
        upstream_device = netbox_device()

        self.update(upstream_device)

        assert upstream_device.updates() == {}

    def test_an_unchanged_device_should_keep_its_related_records(self):
        """Later steps of the sync read these, e.g. device_type.id for assets"""
        upstream_device = netbox_device()

        self.update(upstream_device)

        assert upstream_device.device_type.id == 3
        assert upstream_device.role.slug == "switch"
        assert upstream_device.site.id == 1
        assert upstream_device.location.id == 4

    def test_a_changed_device_type_should_be_sent_as_an_id(self):
        upstream_device = netbox_device()
        new_type = Record({"id": 7, "model": "C9300X"}, MagicMock(), MagicMock())

        self.update(upstream_device, device_type=new_type)

        assert upstream_device.updates() == {"device_type": 7}

    def test_a_changed_tenant_and_status_should_be_sent(self):
        upstream_device = netbox_device(
            tenant={"id": 9, "name": "other"},
            status={"value": "planned", "label": "Planned"},
        )

        self.update(upstream_device)

        assert upstream_device.updates() == {"tenant": 1, "status": "active"}


def nav_server_info(url):
    return NavServerInfo(id=1, url=url, owner_id=1, tenant_id=1)


class TestFetchingFromUnreachableNavServers:
    def fetch(self, *nav_servers, failing_parser="LocationHierarchyParser"):
        def location_parser(nav_server, token):
            parser = MagicMock()
            if failing_parser == "LocationHierarchyParser" and (
                nav_server.url == UNREACHABLE
            ):
                parser.get_sites_and_locations.side_effect = ConnectionError("down")
            else:
                site = make_site()
                site.name = nav_server.url
                parser.get_sites_and_locations.return_value = (
                    {site.name: site},
                    {},
                )
            return parser

        def entity_parser(nav_server, token, locations, netbox_tenants, categories):
            parser = MagicMock()
            if failing_parser == "EntityParser" and nav_server.url == UNREACHABLE:
                parser.parse.side_effect = ConnectionError("down")
            else:
                parser.parse.return_value = []
            return parser

        fetcher = NavFetcher(
            private_key="",
            expiry_delta=timedelta(),
            issuer="",
            netbox_tenants={},
            categories=[],
        )
        fetcher.generate_nav_token = lambda aud: "token"
        with (
            patch("navsync.parser.LocationHierarchyParser", location_parser),
            patch("navsync.parser.EntityParser", entity_parser),
        ):
            return fetcher.fetch([nav_server_info(url) for url in nav_servers])

    def test_an_unreachable_nav_server_should_not_stop_the_sync(self):
        result = self.fetch(UNREACHABLE, REACHABLE)

        assert [site.name for site in result.sites] == [REACHABLE]

    def test_an_unreachable_nav_server_should_be_reported(self):
        result = self.fetch(UNREACHABLE, REACHABLE)

        assert result.unreachable_nav_servers == {UNREACHABLE}

    def test_a_nav_server_failing_halfway_should_contribute_nothing(self):
        result = self.fetch(UNREACHABLE, REACHABLE, failing_parser="EntityParser")

        assert [site.name for site in result.sites] == [REACHABLE]
        assert result.unreachable_nav_servers == {UNREACHABLE}

    def test_reachable_nav_servers_should_not_be_reported(self):
        result = self.fetch(REACHABLE)

        assert result.unreachable_nav_servers == set()
