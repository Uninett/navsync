from unittest.mock import MagicMock

from navsync.parser import EntityParser


def navbox(sysname, category):
    box = MagicMock()
    box.sysname = sysname
    box.category = category
    return box


def make_parser(navboxes, categories):
    parser = EntityParser.__new__(EntityParser)
    parser._nav_api = MagicMock()
    parser._nav_api.get_navboxes.return_value = navboxes
    parser._locations = {"room": MagicMock()}
    parser._categories = categories
    parser._org_tenant_resolver = MagicMock()
    parser._org_tenant_resolver.resolve.return_value = 1
    parser._try_parse_standard_virtual_chassis = lambda box, tenant_id: box.sysname
    return parser


class TestCategoryFiltering:
    def test_navboxes_in_a_configured_category_should_be_parsed(self):
        parser = make_parser([navbox("sw-1", "SW")], categories=["SW"])

        assert parser.parse() == ["sw-1"]

    def test_navboxes_in_other_categories_should_be_skipped(self):
        parser = make_parser(
            [navbox("sw-1", "SW"), navbox("srv-1", "SRV")], categories=["SW"]
        )

        assert parser.parse() == ["sw-1"]
