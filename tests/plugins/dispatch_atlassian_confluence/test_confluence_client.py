"""Which 5.x client each deployment type gets, and how it authenticates (#214)."""

import base64

import pytest
from atlassian import Confluence, ConfluenceServer, ConfluenceV2

from dispatch.plugins.dispatch_atlassian_confluence.client import confluence_api
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import (
    PASSWORD,
    TEMPLATE_ID,
    USERNAME,
    build_configuration,
)


@pytest.mark.parametrize(
    "hosting_type,expected",
    [("cloud", ConfluenceV2), ("server", ConfluenceServer)],
)
def test_hosting_type_selects_the_client_class(hosting_type, expected):
    api = confluence_api(build_configuration(hosting_type))
    assert type(api.client) is expected


def test_the_two_clients_are_not_interchangeable():
    """Neither class inherits the other, so picking the wrong one is fatal, not
    merely suboptimal -- which is what makes the selection worth asserting."""
    assert not issubclass(ConfluenceV2, ConfluenceServer)
    assert not issubclass(ConfluenceServer, ConfluenceV2)


def test_cloud_reads_pages_from_v2_under_the_wiki_context_path(confluence):
    api = confluence_api(build_configuration("cloud"))
    assert api.client.url == "https://dispatch-tests.atlassian.net/wiki"

    api.get_page(TEMPLATE_ID)

    request = confluence.last("GET")
    assert request.url.startswith(
        f"https://dispatch-tests.atlassian.net/wiki/api/v2/pages/{TEMPLATE_ID}"
    )
    assert "body-format=storage" in request.query


def test_server_reads_pages_from_v1_with_no_context_path(confluence):
    api = confluence_api(build_configuration("server"))
    assert api.client.url == "https://confluence.internal.example.com"

    api.get_page(TEMPLATE_ID)

    request = confluence.last("GET")
    assert request.url.startswith(
        f"https://confluence.internal.example.com/rest/api/content/{TEMPLATE_ID}"
    )
    assert "expand=body.storage" in request.query


def test_passing_hosting_type_as_the_cloud_flag_no_longer_works():
    """The 4.x call shape this migration replaced, asserted so a future bump
    that quietly restores it is noticed.

    `Confluence(cloud=...)` in 5.x routes every truthy value -- including the
    string "server" -- to a class carrying none of the methods Dispatch calls.
    Both hosting types are non-empty strings, which is why #214 broke every
    deployment rather than only Cloud ones.
    """
    for hosting_type in ("cloud", "server"):
        client = Confluence(
            url="https://dispatch-tests.atlassian.net/",
            username=USERNAME,
            password=PASSWORD,
            cloud=hosting_type,
        )
        for method in ("create_page", "get_page_by_id", "update_page"):
            assert not hasattr(client, method), f"{method} unexpectedly present for {hosting_type}"


def test_credentials_reach_the_wire_as_basic_auth(confluence, hosting_type):
    api = confluence_api(build_configuration(hosting_type))
    api.get_page(TEMPLATE_ID)

    header = confluence.last("GET").headers["Authorization"]
    scheme, encoded = header.split(" ", 1)
    assert scheme == "Basic"
    # get_secret_value(), not str(SecretStr): the latter is "**********", which
    # authenticates against nothing and would still look like a token here.
    assert base64.b64decode(encoded).decode() == f"{USERNAME}:{PASSWORD}"
