"""What ConfluencePagePlugin actually sends and returns, per platform (#214)."""

import pytest

from tests.plugins.dispatch_atlassian_confluence.conftest import storage_plugin
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import (
    PARENT_ID,
    SPACE_ID,
    SPACE_KEY,
    TEMPLATE_BODY,
    TEMPLATE_ID,
)


def test_create_file_creates_a_page_in_the_configured_space(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    request = confluence.last("POST")
    assert request.body["title"] == "Dispatch Incident"
    assert "<ac:structured-macro" in request.body["body"]["storage"]["value"]
    if hosting_type == "cloud":
        assert request.path == "/wiki/api/v2/pages"
        assert request.body["parentId"] == PARENT_ID
    else:
        assert request.path == "/rest/api/content"
        assert request.body["space"] == {"key": SPACE_KEY}
        assert request.body["ancestors"] == [{"type": "page", "id": PARENT_ID}]


def test_cloud_resolves_the_space_key_to_the_numeric_id_v2_requires(confluence):
    """v2 rejects a space key in `spaceId`, and the configured value is a key.

    A migration that forwarded the key would construct fine, issue a real
    request, and 400 -- exactly the class of failure #214 is about.
    """
    plugin = storage_plugin("cloud")

    plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    lookup = confluence.last("GET")
    assert lookup.path == "/wiki/api/v2/spaces"
    assert f"keys={SPACE_KEY}" in lookup.query

    assert confluence.last("POST").body["spaceId"] == str(SPACE_ID)


def test_server_keeps_the_editor_and_width_metadata_v1_supports(confluence):
    """`editor`/`full_width` survive on ConfluenceServer and have no v2
    equivalent, so only the Server payload should still carry them."""
    plugin = storage_plugin("server")

    plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    properties = confluence.last("POST").body["metadata"]["properties"]
    assert properties["editor"] == {"value": "v2"}
    assert properties["content-appearance-published"] == {"value": "fixed-width"}


def test_cloud_sends_no_editor_metadata(confluence):
    """v2 pages are always created in the new editor; there is no field for it."""
    plugin = storage_plugin("cloud")

    plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    assert "metadata" not in confluence.last("POST").body


def test_create_file_returns_the_new_page_for_dispatch_storage(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    result = plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    assert result["id"] == "900001"
    assert result["name"] == "Dispatch Incident"
    assert result["description"] == ""
    if hosting_type == "cloud":
        assert result["weblink"] == (
            f"https://dispatch-tests.atlassian.net/wiki/spaces/{SPACE_KEY}"
            "/pages/900001/Dispatch Incident"
        )
    else:
        assert result["weblink"] == (
            "https://confluence.internal.example.com/pages/viewpage.action?pageId=900001"
        )


def test_create_file_accepts_the_keyword_core_calls_it_with(confluence, hosting_type):
    """dispatch.storage.flows and dispatch.document.flows both call
    `create_file(parent_id=...)`. The parameter was named `drive_id`, so every
    call raised TypeError and was swallowed by the caller's except."""
    plugin = storage_plugin(hosting_type)

    result = plugin.create_file(
        parent_id=SPACE_KEY, name="Dispatch Incident", participants=["a@example.com"]
    )

    assert result is not None
    assert confluence.requests


def test_create_file_declines_unsupported_file_types(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    assert plugin.create_file(parent_id=SPACE_KEY, name="x", file_type="spreadsheet") is None
    assert confluence.requests == []


def test_copy_file_sends_the_template_storage_body_not_the_body_object(confluence, hosting_type):
    """The template's storage XHTML is a string. Passing the whole `body`
    object nests a dict under `body.storage.value`, which both APIs reject."""
    plugin = storage_plugin(hosting_type)

    plugin.copy_file(folder_id="444444", file_id="ignored", name="Incident Review")

    created = confluence.last("POST").body["body"]["storage"]["value"]
    assert created == TEMPLATE_BODY
    assert isinstance(created, str)


def test_copy_file_reads_the_configured_template(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    plugin.copy_file(folder_id="444444", file_id="ignored", name="Incident Review")

    assert any(TEMPLATE_ID in r.path for r in confluence.requests if r.method == "GET")


def test_copy_file_creates_the_page_under_the_given_folder(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    result = plugin.copy_file(folder_id="444444", file_id="ignored", name="Incident Review")

    request = confluence.last("POST")
    if hosting_type == "cloud":
        assert request.body["parentId"] == "444444"
        assert request.body["spaceId"] == str(SPACE_ID)
    else:
        assert request.body["ancestors"] == [{"type": "page", "id": "444444"}]
        assert request.body["space"] == {"key": SPACE_KEY}
    assert result["id"] == "900001"
    assert result["name"] == "Incident Review"


@pytest.mark.parametrize("status", [403, 404, 500])
def test_create_file_returns_none_and_logs_when_confluence_fails(
    confluence, hosting_type, caplog, status
):
    """Dispatch's storage flow checks for a falsy return; an exception escaping
    the plugin would abort the incident flow instead."""
    plugin = storage_plugin(hosting_type)
    confluence.fail_with(status)

    assert plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident") is None
    assert "Exception happened while creating page" in caplog.text


@pytest.mark.parametrize("status", [403, 404, 500])
def test_copy_file_returns_none_and_logs_when_confluence_fails(
    confluence, hosting_type, caplog, status
):
    plugin = storage_plugin(hosting_type)
    confluence.fail_with(status)

    assert plugin.copy_file(folder_id="444444", file_id="x", name="Incident Review") is None
    assert "Exception happened while creating page" in caplog.text


def test_move_file_is_a_no_op_that_keeps_the_storage_interface(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    assert plugin.move_file(new_folder_id="1", file_id="2") == {}
    assert confluence.requests == []


def test_create_file_cannot_make_sub_pages_from_a_page_id(confluence, hosting_type, caplog):
    """Core's storage flow calls create_file three times: once with the project
    storage root (a space key) and twice more with the page id it just got back,
    for "Logs" and "Screengrabs". Confluence has no folders and this plugin uses
    the argument as a space key, so those two calls cannot succeed.

    What matters is that they fail the way callers expect -- a logged error and
    a falsy return, which `dispatch.storage.flows` checks for -- rather than
    raising into the incident flow or silently filing pages in the wrong space.
    """
    plugin = storage_plugin(hosting_type)

    assert plugin.create_file(parent_id="900001", name="Logs") is None
    assert "Exception happened while creating page" in caplog.text


def test_copy_file_weblink_names_the_space_the_page_was_created_in(confluence, hosting_type):
    """copy_file creates the page in the configured space, so the weblink has to
    name that space and not the parent page it was filed under."""
    plugin = storage_plugin(hosting_type)

    result = plugin.copy_file(folder_id="444444", file_id="x", name="Incident Review")

    if hosting_type == "cloud":
        assert result["weblink"] == (
            f"https://dispatch-tests.atlassian.net/wiki/spaces/{SPACE_KEY}"
            "/pages/900001/Incident Review"
        )
    else:
        assert result["weblink"] == (
            "https://confluence.internal.example.com/pages/viewpage.action?pageId=900001"
        )


def test_server_weblink_survives_an_instance_under_a_context_path(confluence):
    """Confluence Server usually sits under one, and AnyHttpUrl only appends a
    trailing slash when the path is empty."""
    plugin = storage_plugin("server", api_url="https://confluence.example.com/confluence")

    result = plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    assert result["weblink"] == (
        "https://confluence.example.com/confluence/pages/viewpage.action?pageId=900001"
    )


def test_cloud_weblink_is_not_doubled_when_the_url_already_has_the_wiki_path(confluence):
    """The client appends /wiki itself when the configured URL lacks it, so the
    weblink must not assume it is absent."""
    plugin = storage_plugin("cloud", api_url="https://dispatch-tests.atlassian.net/wiki")

    result = plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident")

    assert result["weblink"] == (
        f"https://dispatch-tests.atlassian.net/wiki/spaces/{SPACE_KEY}"
        "/pages/900001/Dispatch Incident"
    )


def test_cloud_failures_are_logged_with_something_actionable(confluence, caplog):
    """The Cloud client raises HTTPError with an empty message, because its
    raise_for_status does not parse v2's error envelope. Logging the exception
    alone leaves an operator with a bare "Exception happened while creating
    page:" and no status to act on."""
    plugin = storage_plugin("cloud")
    confluence.fail_with(403, {"errors": [{"status": 403, "title": "Not permitted"}]})

    assert plugin.create_file(parent_id=SPACE_KEY, name="Dispatch Incident") is None

    assert "403" in caplog.text
