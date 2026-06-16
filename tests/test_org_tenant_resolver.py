from unittest.mock import MagicMock

from navsync.parser import OrgTenantResolver


def make_resolver(nav_orgs, default_tenant_id=1, netbox_tenants=None):
    nav_api = MagicMock()
    nav_api.get.return_value = nav_orgs
    return OrgTenantResolver(
        nav_api=nav_api,
        default_tenant_id=default_tenant_id,
        netbox_tenants=netbox_tenants or {},
    )


def nav_org(id, parent=None):
    return {"id": id, "parent": parent}


class TestCnaasOrg:
    def test_cnaas_org_should_resolve_to_default_tenant(self):
        resolver = make_resolver([nav_org("cnaas")], default_tenant_id=42)
        assert resolver.resolve("cnaas") == 42

    def test_org_not_in_cnaas_subtree_should_resolve_to_none(self):
        resolver = make_resolver([nav_org("cnaas"), nav_org("other")])
        assert resolver.resolve("other") is None

    def test_unknown_org_should_resolve_to_none(self):
        resolver = make_resolver([nav_org("cnaas")])
        assert resolver.resolve("nonexistent") is None


class TestDirectCnaasChild:
    def test_when_matching_netbox_tenant_exists_then_it_should_be_used(self):
        resolver = make_resolver(
            [nav_org("cnaas"), nav_org("khio", parent="cnaas")],
            default_tenant_id=1,
            netbox_tenants={"khio": 99},
        )
        assert resolver.resolve("khio") == 99

    def test_when_no_matching_netbox_tenant_exists_then_default_should_be_used(self):
        resolver = make_resolver(
            [nav_org("cnaas"), nav_org("khio", parent="cnaas")],
            default_tenant_id=1,
            netbox_tenants={},
        )
        assert resolver.resolve("khio") == 1


class TestIndirectCnaasChild:
    def test_grandchild_of_cnaas_should_resolve_using_its_own_tenant(self):
        resolver = make_resolver(
            [
                nav_org("cnaas"),
                nav_org("mid", parent="cnaas"),
                nav_org("leaf", parent="mid"),
            ],
            default_tenant_id=1,
            netbox_tenants={"leaf": 77},
        )
        assert resolver.resolve("leaf") == 77

    def test_grandchild_without_matching_tenant_should_fall_back_to_default(self):
        resolver = make_resolver(
            [
                nav_org("cnaas"),
                nav_org("mid", parent="cnaas"),
                nav_org("leaf", parent="mid"),
            ],
            default_tenant_id=5,
            netbox_tenants={},
        )
        assert resolver.resolve("leaf") == 5

    def test_sibling_of_cnaas_should_not_be_in_subtree(self):
        resolver = make_resolver(
            [
                nav_org("root"),
                nav_org("cnaas", parent="root"),
                nav_org("sibling", parent="root"),
            ],
        )
        assert resolver.resolve("sibling") is None
