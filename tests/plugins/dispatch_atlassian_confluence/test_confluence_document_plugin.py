"""What ConfluencePageDocPlugin actually substitutes and sends (#214)."""

import pytest
from atlassian.errors import ApiError
from requests import HTTPError

from tests.plugins.dispatch_atlassian_confluence.conftest import document_plugin
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import TEMPLATE_ID


def test_update_substitutes_placeholders_into_the_stored_body(confluence, hosting_type):
    """The template body is `<p>Commander: {{commander}} Status: {{status}}</p>`;
    the plugin wraps each kwarg name in braces before replacing."""
    plugin = document_plugin(hosting_type)

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace", status="Stable")

    body = confluence.last("PUT").body["body"]["storage"]["value"]
    assert body == "<p>Commander: Ada Lovelace Status: Stable</p>"


def test_update_leaves_placeholders_alone_when_a_value_is_empty(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace", status="")

    body = confluence.last("PUT").body["body"]["storage"]["value"]
    assert body == "<p>Commander: Ada Lovelace Status: {{status}}</p>"


def test_update_ignores_keys_the_template_does_not_mention(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace", unrelated="ignored")

    body = confluence.last("PUT").body["body"]["storage"]["value"]
    assert "ignored" not in body


def test_update_keeps_the_existing_page_title(confluence, hosting_type):
    """Both APIs require a title on update; sending the wrong one renames the
    incident document."""
    plugin = document_plugin(hosting_type)

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace")

    assert confluence.last("PUT").body["title"] == "Incident Template"


def test_update_targets_the_document_on_the_platforms_own_endpoint(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace")

    path = confluence.last("PUT").path
    if hosting_type == "cloud":
        assert path == f"/wiki/api/v2/pages/{TEMPLATE_ID}"
    else:
        assert path == f"/rest/api/content/{TEMPLATE_ID}"


def test_update_advances_the_page_version_from_wherever_it_was(confluence, hosting_type):
    """Both APIs reject an update that does not move the version forward; v2
    makes the caller supply it, v1 reads it from the page history."""
    plugin = document_plugin(hosting_type)

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace")
    plugin.update(TEMPLATE_ID, status="Stable")

    # A second update rules out a hardcoded 2: the version has to come from
    # the page as it stands, or Confluence answers 409.
    assert confluence.last("PUT").body["version"]["number"] == 3


def test_update_returns_the_updated_page(confluence, hosting_type):
    plugin = document_plugin(hosting_type)

    result = plugin.update(TEMPLATE_ID, commander="Ada Lovelace")

    assert result["id"] == TEMPLATE_ID
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
        plugin.update(TEMPLATE_ID, commander="Ada Lovelace")

    assert all(request.method != "PUT" for request in confluence.requests)


def test_server_skips_the_write_when_nothing_changed(confluence):
    """A documented platform difference: the v1 client compares the stored body
    first and returns without a PUT when it matches, where v2 always writes."""
    plugin = document_plugin("server")

    plugin.update(TEMPLATE_ID, commander="")

    assert all(request.method != "PUT" for request in confluence.requests)


def test_cloud_writes_even_when_nothing_changed(confluence):
    plugin = document_plugin("cloud")

    plugin.update(TEMPLATE_ID, commander="")

    assert confluence.last("PUT").body["body"]["storage"]["value"] == (
        "<p>Commander: {{commander}} Status: {{status}}</p>"
    )


def test_cloud_never_asks_for_a_body_format_the_api_does_not_define(confluence):
    """Left to itself the Cloud client reads the page back with get_body=False
    to learn its version, and renders that as `body-format=none` -- not a member
    of PrimaryBodyRepresentationSingle, so Confluence rejects the request and
    the document is never written. Handing it the version we already read keeps
    that path unused."""
    plugin = document_plugin("cloud")

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace")

    assert all(r.params.get("body-format") != "none" for r in confluence.requests)


def test_cloud_reads_the_document_once(confluence):
    """The page fetched to substitute into is the same one the update needs a
    version and title from. Server reads again regardless: its client compares
    the stored body before writing, which is what lets it skip a no-op PUT.
    """
    plugin = document_plugin("cloud")

    plugin.update(TEMPLATE_ID, commander="Ada Lovelace")

    reads = [r for r in confluence.requests if r.method == "GET" and "/history" not in r.path]
    assert len(reads) == 1
