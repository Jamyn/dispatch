"""What ConfluencePageDocPlugin actually substitutes and sends (#214)."""

import pytest
from atlassian.errors import ApiError
from requests import HTTPError

from tests.plugins.dispatch_atlassian_confluence.conftest import document_plugin
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import ROOT_PAGE_ID


def test_update_substitutes_placeholders_into_the_stored_body(confluence, hosting_type):
    """The template body is `<p>Commander: {{commander}} Status: {{status}}</p>`;
    the plugin wraps each kwarg name in braces before replacing."""
    plugin = document_plugin(hosting_type)

    plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace", status="Stable")

    body = confluence.last("PUT").body["body"]["storage"]["value"]
    assert body == "<p>Commander: Ada Lovelace Status: Stable</p>"


def test_update_leaves_placeholders_alone_when_a_value_is_empty(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace", status="")

    body = confluence.last("PUT").body["body"]["storage"]["value"]
    assert body == "<p>Commander: Ada Lovelace Status: {{status}}</p>"


def test_update_ignores_keys_the_template_does_not_mention(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace", unrelated="ignored")

    body = confluence.last("PUT").body["body"]["storage"]["value"]
    assert "ignored" not in body


def test_update_keeps_the_existing_page_title(confluence, hosting_type):
    """Both APIs require a title on update; sending the wrong one renames the
    incident document."""
    plugin = document_plugin(hosting_type)

    plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace")

    assert confluence.last("PUT").body["title"] == "Incident Template"


def test_update_targets_the_document_on_the_platforms_own_endpoint(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace")

    path = confluence.last("PUT").path
    if hosting_type == "cloud":
        assert path == f"/wiki/api/v2/pages/{ROOT_PAGE_ID}"
    else:
        assert path == f"/rest/api/content/{ROOT_PAGE_ID}"


def test_update_advances_the_page_version(confluence, hosting_type):
    """Both APIs reject an update that does not move the version forward; v2
    makes the caller supply it, v1 reads it from the page history."""
    plugin = document_plugin(hosting_type)

    plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace")

    assert confluence.last("PUT").body["version"]["number"] == 2


def test_update_returns_the_updated_page(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    result = plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace")

    assert result["id"] == ROOT_PAGE_ID
    assert result["body"]["storage"]["value"] == "<p>Commander: Ada Lovelace Status: {{status}}</p>"


@pytest.mark.parametrize("status", [403, 404, 500])
def test_update_propagates_confluence_failures(confluence, hosting_type, status):
    """`DocumentPlugin.update` has no except of its own, and its callers treat a
    raised error as the document flow failing. Swallowing it here would report
    an unsubstituted document as successfully written.

    Both exception families are accepted because the two clients differ: the
    Server client translates 403/404 into atlassian's own ApiError subclasses,
    while the Cloud client lets requests' HTTPError through.
    """
    plugin = document_plugin(hosting_type)
    confluence.fail_with(status)

    with pytest.raises((HTTPError, ApiError)):
        plugin.update(ROOT_PAGE_ID, commander="Ada Lovelace")

    assert all(request.method != "PUT" for request in confluence.requests)


def test_server_skips_the_write_when_nothing_changed(confluence):
    """A documented platform difference: the v1 client compares the stored body
    first and returns without a PUT when it matches, where v2 always writes."""
    plugin = document_plugin("server")

    plugin.update(ROOT_PAGE_ID, commander="")

    assert all(request.method != "PUT" for request in confluence.requests)


def test_cloud_writes_even_when_nothing_changed(confluence):
    plugin = document_plugin("cloud")

    plugin.update(ROOT_PAGE_ID, commander="")

    assert confluence.last("PUT").body["body"]["storage"]["value"] == (
        "<p>Commander: {{commander}} Status: {{status}}</p>"
    )
