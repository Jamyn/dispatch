"""What ConfluencePagePlugin actually sends and returns, per platform (#214, #242)."""

import pytest
from atlassian.errors import ApiError
from requests import HTTPError

from dispatch.plugins.dispatch_atlassian_confluence.client import ConfluenceError
from tests.plugins.dispatch_atlassian_confluence.conftest import storage_plugin
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import (
    REPORTED_BASE,
    ROOT_PAGE_ID,
    SPACE_ID,
    SPACE_KEY,
    TEMPLATE_BODY,
    TEMPLATE_ID,
)


def test_create_file_creates_a_child_of_the_page_it_is_given(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    request = confluence.last("POST")
    assert request.body["title"] == "Dispatch Incident"
    assert "<ac:structured-macro" in request.body["body"]["storage"]["value"]
    if hosting_type == "cloud":
        assert request.path == "/wiki/api/v2/pages"
        assert request.body["parentId"] == ROOT_PAGE_ID
        assert request.body["spaceId"] == str(SPACE_ID)
    else:
        assert request.path == "/rest/api/content"
        assert request.body["ancestors"] == [{"type": "page", "id": ROOT_PAGE_ID}]
        assert request.body["space"] == {"key": SPACE_KEY}


def test_create_file_reads_the_space_off_the_parent_page(confluence, hosting_type):
    """Neither API infers the space from the parent: v2 requires spaceId even
    alongside parentId, and v1 requires the space key. Configuring it
    separately is what made the identifier a space key rather than a page id,
    so it has to come from the parent instead."""
    plugin = storage_plugin(hosting_type)

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    lookup = confluence.requests[0]
    assert lookup.method == "GET"
    if hosting_type == "cloud":
        assert lookup.path == f"/wiki/api/v2/pages/{ROOT_PAGE_ID}"
        # PrimaryBodyRepresentationSingle has no `none` member, so asking the
        # client for no body at all sends a body-format the API rejects.
        assert "body-format" not in lookup.params
    else:
        assert lookup.path == f"/rest/api/content/{ROOT_PAGE_ID}"
        assert lookup.params["expand"] == "space"


def test_create_file_makes_sub_pages_beneath_the_page_it_just_created(confluence, hosting_type):
    """The sequence dispatch.storage.flows really runs: the incident's own page
    under the project storage root, then "Logs" and "Screengrabs" under the page
    it got back. Before #242 the plugin read that argument as a space key, so
    only the first of the three could succeed."""
    plugin = storage_plugin(hosting_type)

    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")
    logs = plugin.create_file(parent_id=incident["id"], name="Logs")
    screengrabs = plugin.create_file(parent_id=incident["id"], name="Screengrabs")

    assert confluence.page(incident["id"]).parent_id == ROOT_PAGE_ID
    assert confluence.page(logs["id"]).parent_id == incident["id"]
    assert confluence.page(screengrabs["id"]).parent_id == incident["id"]
    assert len({incident["id"], logs["id"], screengrabs["id"]}) == 3


def test_two_subjects_can_each_have_their_own_sub_pages(confluence, hosting_type):
    """Confluence titles are unique per space and the folder names come from
    project settings, so every incident asks for the same two. Creating them
    verbatim works exactly once and then 400s for the rest of the space's life."""
    plugin = storage_plugin(hosting_type)

    first = plugin.create_file(parent_id=ROOT_PAGE_ID, name="INC-0001")
    second = plugin.create_file(parent_id=ROOT_PAGE_ID, name="INC-0002")
    logs_one = plugin.create_file(parent_id=first["id"], name="Logs")
    logs_two = plugin.create_file(parent_id=second["id"], name="Logs")

    assert logs_one is not None and logs_two is not None
    assert confluence.page(logs_one["id"]).parent_id == first["id"]
    assert confluence.page(logs_two["id"]).parent_id == second["id"]
    assert confluence.page(logs_one["id"]).title != confluence.page(logs_two["id"]).title


def test_a_sub_page_is_titled_for_the_page_it_belongs_to(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="INC-0001")

    logs = plugin.create_file(parent_id=incident["id"], name="Logs")

    assert confluence.last("POST").body["title"] == "INC-0001 - Logs"
    # Dispatch keeps calling it what it asked for; only Confluence sees the
    # qualified title, so the folder's name in the UI is unchanged.
    assert logs["name"] == "Logs"


def test_the_subjects_own_page_is_titled_exactly_what_dispatch_asked_for(confluence, hosting_type):
    """It hangs off the configured root and is named after the subject, which
    is already unique -- qualifying it would read "Incidents - INC-0001"."""
    plugin = storage_plugin(hosting_type)

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="INC-0001")

    assert confluence.last("POST").body["title"] == "INC-0001"


def test_a_blank_document_is_not_retitled_for_its_folder(confluence, hosting_type):
    """`create_document` already prefixes the subject's name, so qualifying
    again would produce "INC-0001 - INC-0001 - Incident Review"."""
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="INC-0001")

    plugin.create_file(
        parent_id=incident["id"], name="INC-0001 - Incident Review", file_type="document"
    )

    assert confluence.last("POST").body["title"] == "INC-0001 - Incident Review"


def test_create_file_makes_the_blank_document_the_document_flow_asks_for(confluence, hosting_type):
    """dispatch.document.flows falls back to create_file(file_type="document")
    with the incident's storage id when a document type has no template."""
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    document = plugin.create_file(
        parent_id=incident["id"], name="Dispatch Incident - Review", file_type="document"
    )

    assert document is not None
    assert confluence.page(document["id"]).parent_id == incident["id"]
    # A document is not a folder listing: the children macro belongs on the
    # page that stands in for the folder, not on the document inside it.
    assert "<ac:structured-macro" not in confluence.last("POST").body["body"]["storage"]["value"]


def test_server_keeps_the_editor_and_width_metadata_v1_supports(confluence):
    """`editor`/`full_width` survive on ConfluenceServer and have no v2
    equivalent, so only the Server payload should still carry them."""
    plugin = storage_plugin("server")

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    properties = confluence.last("POST").body["metadata"]["properties"]
    assert properties["editor"] == {"value": "v2"}
    assert properties["content-appearance-published"] == {"value": "fixed-width"}


def test_cloud_sends_no_editor_metadata(confluence):
    """v2 pages are always created in the new editor; there is no field for it."""
    plugin = storage_plugin("cloud")

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert "metadata" not in confluence.last("POST").body


def test_nothing_looks_a_space_up_by_key_any_more(confluence, hosting_type):
    """The space comes off the parent page, so the key is neither configured
    nor resolved. A lookup reappearing here means the identifier drifted back
    towards being a space key."""
    plugin = storage_plugin(hosting_type)

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert all("/spaces" not in request.path for request in confluence.requests)
    assert all("/rest/api/space" not in request.path for request in confluence.requests)


def test_create_file_returns_the_new_page_for_dispatch_storage(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert result["id"] == "900001"
    assert result["name"] == "Dispatch Incident"
    assert result["description"] == ""


def test_create_file_accepts_the_keyword_core_calls_it_with(confluence, hosting_type):
    """dispatch.storage.flows and dispatch.document.flows both call
    `create_file(parent_id=...)`. The parameter was named `drive_id`, so every
    call raised TypeError and was swallowed by the caller's except."""
    plugin = storage_plugin(hosting_type)

    result = plugin.create_file(
        parent_id=ROOT_PAGE_ID, name="Dispatch Incident", participants=["a@example.com"]
    )

    assert result is not None
    assert confluence.requests


def test_create_file_declines_unsupported_file_types(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    assert plugin.create_file(parent_id=ROOT_PAGE_ID, name="x", file_type="spreadsheet") is None
    assert confluence.requests == []


def test_copy_file_sends_the_template_storage_body_not_the_body_object(confluence, hosting_type):
    """The template's storage XHTML is a string. Passing the whole `body`
    object nests a dict under `body.storage.value`, which both APIs reject."""
    plugin = storage_plugin(hosting_type)

    plugin.copy_file(folder_id=ROOT_PAGE_ID, file_id=TEMPLATE_ID, name="Incident Review")

    created = confluence.last("POST").body["body"]["storage"]["value"]
    assert created == TEMPLATE_BODY
    assert isinstance(created, str)


def test_copy_file_copies_the_template_it_was_given(confluence, hosting_type):
    """Dispatch resolves a different template per document -- the incident
    type's, the executive report's, the form export's -- and passes it as
    `file_id`. The plugin used to ignore it and copy one configured page, so
    every executive report was a copy of the incident template."""
    plugin = storage_plugin(hosting_type)

    plugin.copy_file(folder_id=ROOT_PAGE_ID, file_id=ROOT_PAGE_ID, name="Executive Report")

    read = [r.path for r in confluence.requests if r.method == "GET"]
    assert any(ROOT_PAGE_ID in path for path in read)
    assert not any(TEMPLATE_ID in path for path in read)
    assert confluence.last("POST").body["body"]["storage"]["value"] == "<p>Incident home</p>"


def test_copy_file_creates_the_page_under_the_given_folder(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    result = plugin.copy_file(folder_id=incident["id"], file_id=TEMPLATE_ID, name="Incident Review")

    request = confluence.last("POST")
    if hosting_type == "cloud":
        assert request.body["parentId"] == incident["id"]
        assert request.body["spaceId"] == str(SPACE_ID)
    else:
        assert request.body["ancestors"] == [{"type": "page", "id": incident["id"]}]
        assert request.body["space"] == {"key": SPACE_KEY}
    assert confluence.page(result["id"]).parent_id == incident["id"]
    assert result["name"] == "Incident Review"


def test_copy_file_links_to_the_new_page_not_the_template(confluence, hosting_type):
    """Both pages come back from the API in this method, and the weblink is
    persisted as the document's own link."""
    plugin = storage_plugin(hosting_type)

    result = plugin.copy_file(folder_id=ROOT_PAGE_ID, file_id=TEMPLATE_ID, name="Incident Review")

    assert result["id"] in result["weblink"] or "Incident+Review" in result["weblink"]
    assert TEMPLATE_ID not in result["weblink"]
    assert "Incident+Template" not in result["weblink"]


def test_copy_file_needs_no_second_call_to_place_the_page(confluence, hosting_type):
    """`move_file_confluence` used to follow every copy with a PUT to a v1
    `/move/append/` endpoint that Cloud has removed and Server does not serve
    under `/wiki`. The create already files the page, and `requests` does not
    raise on 4xx, so the move only ever failed silently."""
    plugin = storage_plugin(hosting_type)

    plugin.copy_file(folder_id=ROOT_PAGE_ID, file_id=TEMPLATE_ID, name="Incident Review")

    assert all("/move/" not in request.path for request in confluence.requests)
    assert all(request.method != "PUT" for request in confluence.requests)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_create_file_returns_none_and_logs_when_confluence_fails(
    confluence, hosting_type, caplog, status
):
    """Dispatch's storage flow checks for a falsy return; an exception escaping
    the plugin would abort the incident flow instead."""
    plugin = storage_plugin(hosting_type)
    confluence.fail_with(status)

    assert plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident") is None
    assert "Exception happened while creating page" in caplog.text


@pytest.mark.parametrize("status", [403, 404, 500])
def test_copy_file_returns_none_and_logs_when_confluence_fails(
    confluence, hosting_type, caplog, status
):
    plugin = storage_plugin(hosting_type)
    confluence.fail_with(status)

    assert (
        plugin.copy_file(folder_id=ROOT_PAGE_ID, file_id=TEMPLATE_ID, name="Incident Review")
        is None
    )
    assert "Exception happened while creating page" in caplog.text


def test_create_file_fails_cleanly_when_the_parent_page_is_gone(confluence, hosting_type, caplog):
    """A stale root id in the configuration, or a page an operator deleted. The
    space lookup is the first thing to notice, and the flow needs a falsy
    return rather than an exception."""
    plugin = storage_plugin(hosting_type, root_id="404404")

    assert plugin.create_file(parent_id="404404", name="Dispatch Incident") is None
    assert "Exception happened while creating page" in caplog.text
    assert all(request.method != "POST" for request in confluence.requests)


def test_move_file_is_a_no_op_that_keeps_the_storage_interface(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)

    assert plugin.move_file(new_folder_id="1", file_id="2") == {}
    assert confluence.requests == []


# -- weblinks ---------------------------------------------------------------


def test_weblink_comes_from_the_link_confluence_returned(confluence, hosting_type):
    """`_links.webui` carries the space key, which is otherwise a lookup, and
    is the shape each platform's own UI uses."""
    plugin = storage_plugin(hosting_type)

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    if hosting_type == "cloud":
        assert result["weblink"] == (
            f"https://dispatch-tests.atlassian.net/wiki/spaces/{SPACE_KEY}"
            "/pages/900001/Dispatch+Incident"
        )
    else:
        assert result["weblink"] == (
            f"https://confluence.internal.example.com/display/{SPACE_KEY}/Dispatch+Incident"
        )


def test_weblink_keeps_the_host_dispatch_was_configured_with(confluence, hosting_type):
    """Confluence reports its own base URL alongside the relative link, and on
    Server that is whatever the instance was told to think it is -- which need
    not be reachable from where Dispatch's users are."""
    plugin = storage_plugin(hosting_type)

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert REPORTED_BASE not in result["weblink"]


def test_weblink_falls_back_to_the_page_id_when_no_link_comes_back(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)
    confluence.omit_webui = True

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    if hosting_type == "cloud":
        assert result["weblink"] == (
            "https://dispatch-tests.atlassian.net/wiki/pages/viewpage.action?pageId=900001"
        )
    else:
        assert result["weblink"] == (
            "https://confluence.internal.example.com/pages/viewpage.action?pageId=900001"
        )


def test_server_weblink_survives_an_instance_under_a_context_path(confluence):
    """Confluence Server usually sits under one, and AnyHttpUrl only appends a
    trailing slash when the path is empty. v1's `_links.webui` is relative to
    the context path, so it has to be joined onto it and not onto the host."""
    plugin = storage_plugin("server", api_url="https://confluence.example.com/confluence")

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert result["weblink"] == (
        f"https://confluence.example.com/confluence/display/{SPACE_KEY}/Dispatch+Incident"
    )


def test_server_weblink_falls_back_under_a_context_path_too(confluence):
    plugin = storage_plugin("server", api_url="https://confluence.example.com/confluence")
    confluence.omit_webui = True

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert result["weblink"] == (
        "https://confluence.example.com/confluence/pages/viewpage.action?pageId=900001"
    )


def test_cloud_weblink_is_not_doubled_when_the_url_already_has_the_wiki_path(confluence):
    """Dispatch appends the context path itself now, so it must not append a
    second one to a URL that already carries it."""
    plugin = storage_plugin("cloud", api_url="https://dispatch-tests.atlassian.net/wiki")

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert result["weblink"] == (
        f"https://dispatch-tests.atlassian.net/wiki/spaces/{SPACE_KEY}"
        "/pages/900001/Dispatch+Incident"
    )


def test_a_cloud_site_on_a_custom_hostname_still_reaches_the_wiki_context(confluence):
    """The client appends /wiki only for *.atlassian.net, *.jira.com and the
    API gateway, so a Cloud site on a custom domain addressed /api/v2 off the
    domain root. `hosting_type` says it is Cloud; that beats the hostname."""
    plugin = storage_plugin("cloud", api_url="https://wiki.example.com")

    result = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert confluence.last("POST").path == "/wiki/api/v2/pages"
    assert result["weblink"] == (
        f"https://wiki.example.com/wiki/spaces/{SPACE_KEY}/pages/900001/Dispatch+Incident"
    )


def test_a_cloud_gateway_url_is_left_exactly_as_configured(confluence):
    """`api.atlassian.com/ex/confluence/{cloudId}` already carries its own
    context path. Appending /wiki to it addresses a path that does not exist,
    which is why upstream excludes gateway URLs from the same rewrite."""
    plugin = storage_plugin("cloud", api_url="https://api.atlassian.com/ex/confluence/abc-123")

    plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    assert confluence.last("POST").path == "/ex/confluence/abc-123/api/v2/pages"


def test_cloud_failures_are_logged_with_something_actionable(confluence, caplog):
    """The Cloud client raises HTTPError with an empty message, because its
    raise_for_status does not parse v2's error envelope. Logging the exception
    alone leaves an operator with a bare "Exception happened while creating
    page:" and no status to act on."""
    plugin = storage_plugin("cloud")
    confluence.fail_with(403, {"errors": [{"status": 403, "title": "Not permitted"}]})

    assert plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident") is None

    assert "403" in caplog.text


# -- deleting storage -------------------------------------------------------


def test_delete_file_removes_the_page(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    plugin.delete_file(file_id=incident["id"])

    assert incident["id"] not in confluence.pages


def test_delete_file_takes_the_whole_incident_with_it(confluence, hosting_type):
    """Cloud re-parents a deleted page's children onto its grandparent rather
    than removing them, so a delete that does not walk the tree leaves Logs,
    Screengrabs and every document sitting on the storage root."""
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")
    logs = plugin.create_file(parent_id=incident["id"], name="Logs")
    document = plugin.copy_file(folder_id=logs["id"], file_id=TEMPLATE_ID, name="Incident Document")

    plugin.delete_file(file_id=incident["id"])

    for page_id in (incident["id"], logs["id"], document["id"]):
        assert page_id not in confluence.pages
    assert ROOT_PAGE_ID in confluence.pages


def test_delete_file_leaves_the_rest_of_the_space_alone(confluence, hosting_type):
    plugin = storage_plugin(hosting_type)
    keep = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Another Incident")
    keep_logs = plugin.create_file(parent_id=keep["id"], name="Logs")
    remove = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")

    plugin.delete_file(file_id=remove["id"])

    assert keep["id"] in confluence.pages
    assert keep_logs["id"] in confluence.pages
    assert TEMPLATE_ID in confluence.pages


@pytest.mark.parametrize("methods", [(), ("DELETE",)])
def test_delete_file_reports_a_failure_rather_than_swallowing_it(confluence, hosting_type, methods):
    """`delete_storage` logs what this raises. Returning quietly would report
    storage as cleaned up while the pages are still there.

    Refusing only the DELETE is the case that matters: the v1 client issues it
    in a mode that skips raise_for_status, so the refusal arrives as a status
    code rather than an exception."""
    plugin = storage_plugin(hosting_type)
    incident = plugin.create_file(parent_id=ROOT_PAGE_ID, name="Dispatch Incident")
    confluence.fail_with(403, methods=methods)

    # Cloud raises from the client, Server returns a status the wrapper turns
    # into one, and a refused listing raises before either.
    with pytest.raises((HTTPError, ApiError, ConfluenceError)):
        plugin.delete_file(file_id=incident["id"])
    assert incident["id"] in confluence.pages
