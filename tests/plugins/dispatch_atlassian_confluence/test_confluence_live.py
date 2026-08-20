"""Drive the Confluence plugins against real Confluence instances (issue #242).

Everything else in this directory asserts what the plugins *send*. Only
Confluence can say what it *accepts*, and the two are not the same: the
migration in #214 was verified against Atlassian's published v2 OpenAPI spec
and the installed 5.0.3 source, never against a running instance. This suite is
what turns "inferred from the schema" into "observed".

Skipped unless an instance is configured, so it is inert locally and in CI by
default. The two platforms are independent: configure either and its half runs.

Configuration
-------------
Cloud::

    DISPATCH_CONFLUENCE_TEST_CLOUD_URL          https://<site>.atlassian.net
    DISPATCH_CONFLUENCE_TEST_CLOUD_USERNAME     Atlassian account email
    DISPATCH_CONFLUENCE_TEST_CLOUD_PASSWORD     API token, not the password
    DISPATCH_CONFLUENCE_TEST_CLOUD_ROOT_ID      page id everything is created under
    DISPATCH_CONFLUENCE_TEST_CLOUD_TEMPLATE_ID  page id of a template page

Server/Data Center::

    DISPATCH_CONFLUENCE_TEST_SERVER_URL         https://confluence.example.com
    DISPATCH_CONFLUENCE_TEST_SERVER_USERNAME
    DISPATCH_CONFLUENCE_TEST_SERVER_PASSWORD
    DISPATCH_CONFLUENCE_TEST_SERVER_ROOT_ID
    DISPATCH_CONFLUENCE_TEST_SERVER_TEMPLATE_ID

All five of a platform's variables are required; that platform skips unless
every one is set.

Setting an instance up
----------------------
1. Create (or pick) a space the API user can create pages in. On Cloud, get an
   API token from id.atlassian.com -> Security -> API tokens.
2. Create a page in it to hang the tests off, and use its page id as
   ``ROOT_ID``. The page id is in the URL: ``/pages/<id>/<title>``. It is a
   **page** id, not a space key -- that distinction is what issue #242 is
   about.
3. Create a second page whose body contains ``{{commander}}`` somewhere, and
   use its id as ``TEMPLATE_ID``.

Point this at a **throwaway** space. Every test creates real pages.

What this covers that the fake cannot
-------------------------------------
- Confluence accepts the create payload, including that a child page really is
  filed under the parent it was given.
- The space is the parent's, which is the whole of issue #242's item 1.
- Confluence *rejects* ``body-format=none``, which is what the Cloud client
  sends when asked for a page without its body -- an inference from
  ``PrimaryBodyRepresentationSingle`` until now.
- Confluence *rejects* a v2 update with no ``status``, the finding that #243's
  review turned up against the schema alone.
- Confluence *rejects* the body object ``copy_file`` used to send where
  ``body.storage.value`` takes a string, which #242 could only call malformed.
- The ``_links.webui`` weblinks are real links on a real site.

Every page created is deleted afterwards, including when a test fails.
"""

import os
import uuid

import pytest
from requests import HTTPError

from dispatch.plugins.dispatch_atlassian_confluence.client import confluence_api
from dispatch.plugins.dispatch_atlassian_confluence.docs.plugin import ConfluencePageDocPlugin
from dispatch.plugins.dispatch_atlassian_confluence.plugin import (
    ConfluenceConfiguration,
    ConfluencePagePlugin,
)

SETTINGS = ("url", "username", "password", "root_id", "template_id")


def _settings(platform: str) -> dict:
    prefix = f"DISPATCH_CONFLUENCE_TEST_{platform.upper()}_"
    return {name: os.environ.get(prefix + name.upper()) for name in SETTINGS}


class Live:
    """One configured instance, and the pages the test made on it."""

    def __init__(self, platform, settings):
        self.platform = platform
        self.settings = settings
        self.configuration = ConfluenceConfiguration(
            api_url=settings["url"],
            hosting_type=platform,
            username=settings["username"],
            password=settings["password"],
            root_id=settings["root_id"],
            template_id=settings["template_id"],
        )
        self.storage = ConfluencePagePlugin()
        self.storage.configuration = self.configuration
        self.documents = ConfluencePageDocPlugin()
        self.documents.configuration = self.configuration
        self.api = confluence_api(self.configuration)
        self.created = []

    @property
    def root_id(self):
        return self.settings["root_id"]

    def title(self, what: str) -> str:
        """A title nothing else on the instance will collide with.

        Confluence rejects a second page with the same title in one space, so
        a fixed name turns a leaked page from a previous run into a failure of
        the next one.
        """
        return f"Dispatch test {what} {uuid.uuid4().hex[:12]}"

    def track(self, result: dict) -> dict:
        assert result is not None, "the plugin logged an error and returned nothing"
        self.created.append(result["id"])
        return result

    def cleanup(self):
        for page_id in reversed(self.created):
            try:
                if self.platform == "cloud":
                    self.api.client.delete_page(page_id)
                else:
                    self.api.client.remove_page(page_id)
            except Exception as exception:  # noqa: BLE001 - teardown is best effort
                print(f"could not delete {page_id}: {exception}")


@pytest.fixture(params=["cloud", "server"])
def live(request):
    platform = request.param
    settings = _settings(platform)
    missing = sorted(name for name, value in settings.items() if not value)
    if missing:
        prefix = f"DISPATCH_CONFLUENCE_TEST_{platform.upper()}_"
        pytest.skip(f"needs {', '.join(prefix + name.upper() for name in missing)}")

    instance = Live(platform, settings)
    try:
        yield instance
    finally:
        instance.cleanup()


@pytest.fixture
def cloud(live):
    if live.platform != "cloud":
        pytest.skip("Cloud only")
    return live


# -- the hierarchy issue #242 is about --------------------------------------


def test_the_incident_page_is_created_under_the_configured_root(live):
    page = live.track(live.storage.create_file(parent_id=live.root_id, name=live.title("incident")))

    stored = live.api.get_page(page["id"])
    assert stored["title"] == page["name"]
    assert _space_of(live, page["id"]) == _space_of(live, live.root_id)


def test_sub_pages_are_created_under_the_incident_page(live):
    """The three calls `dispatch.storage.flows.create_storage` really makes.
    Before #242 the plugin read this argument as a space key, so the second and
    third could only fail."""
    incident = live.track(
        live.storage.create_file(parent_id=live.root_id, name=live.title("incident"))
    )

    logs = live.track(live.storage.create_file(parent_id=incident["id"], name=live.title("Logs")))
    grabs = live.track(
        live.storage.create_file(parent_id=incident["id"], name=live.title("Screengrabs"))
    )

    for child in (logs, grabs):
        assert _parent_of(live, child["id"]) == incident["id"]


def test_two_incidents_can_each_have_their_own_sub_page(live):
    """Confluence titles are unique per space, and Dispatch names every
    incident's folders identically. Only a real instance enforces this."""
    first = live.track(live.storage.create_file(parent_id=live.root_id, name=live.title("one")))
    second = live.track(live.storage.create_file(parent_id=live.root_id, name=live.title("two")))

    logs_one = live.track(live.storage.create_file(parent_id=first["id"], name="Logs"))
    logs_two = live.track(live.storage.create_file(parent_id=second["id"], name="Logs"))

    assert _parent_of(live, logs_one["id"]) == first["id"]
    assert _parent_of(live, logs_two["id"]) == second["id"]


def test_the_template_is_copied_under_the_incident_page(live):
    incident = live.track(
        live.storage.create_file(parent_id=live.root_id, name=live.title("incident"))
    )

    document = live.track(
        live.storage.copy_file(
            folder_id=incident["id"], file_id=live.settings["template_id"], name=live.title("doc")
        )
    )

    assert _parent_of(live, document["id"]) == incident["id"]
    template = live.api.get_page(live.settings["template_id"])
    assert (
        live.api.get_page(document["id"])["body"]["storage"]["value"]
        == (template["body"]["storage"]["value"])
    )


def test_the_copied_document_can_be_substituted_into(live):
    """The template needs `{{commander}}` in it, per this module's docstring."""
    incident = live.track(
        live.storage.create_file(parent_id=live.root_id, name=live.title("incident"))
    )
    document = live.track(
        live.storage.copy_file(
            folder_id=incident["id"], file_id=live.settings["template_id"], name=live.title("doc")
        )
    )

    live.documents.update(document["id"], commander="Ada Lovelace")

    assert "Ada Lovelace" in live.api.get_page(document["id"])["body"]["storage"]["value"]


def test_the_weblink_points_at_the_site_dispatch_was_configured_with(live):
    page = live.track(live.storage.create_file(parent_id=live.root_id, name=live.title("incident")))

    assert page["weblink"].startswith(str(live.configuration.api_url).rstrip("/"))


def test_a_page_cannot_be_created_under_a_page_that_does_not_exist(live):
    """A stale root id has to fail the way `create_storage` expects -- a falsy
    return -- rather than raising into the incident flow."""
    assert live.storage.create_file(parent_id="1", name=live.title("orphan")) is None


# -- inferences from the schema that only a real instance can settle ---------


def test_confluence_rejects_the_body_object_where_a_string_belongs(live):
    """`copy_file` used to pass the whole `body` dict, which nests an object
    under `body.storage.value`. Objectively malformed; never observed."""
    with pytest.raises(Exception) as failure:
        live.api.create_page(
            parent_id=live.root_id,
            title=live.title("malformed"),
            body={"storage": {"value": "<p>x</p>", "representation": "storage"}},
        )

    assert _status_of(failure.value) == 400


def test_cloud_rejects_a_page_read_that_asks_for_no_body(cloud):
    """`get_body=False` renders as `body-format=none`, and `none` is not a
    member of `PrimaryBodyRepresentationSingle`. The client's own update path
    reads the page that way, which is why `update_page` is handed a version."""
    with pytest.raises(HTTPError) as failure:
        cloud.api.client.get_page_by_id(cloud.root_id, get_body=False)

    assert _status_of(failure.value) == 400


def test_cloud_rejects_an_update_that_omits_the_status(cloud):
    """`PageUpdateRequest` requires it, and the client sends it only when
    asked. Found reviewing #243 against the schema; unobserved until now."""
    page = cloud.track(
        cloud.storage.create_file(parent_id=cloud.root_id, name=cloud.title("incident"))
    )
    current = cloud.api.get_page(page["id"])

    with pytest.raises(HTTPError) as failure:
        cloud.api.client.update_page(
            page_id=page["id"],
            title=current["title"],
            body="<p>rewritten</p>",
            body_format="storage",
            version=current["version"]["number"],
        )

    assert _status_of(failure.value) == 400


def _space_of(live, page_id: str) -> str:
    parent = live.api._parent(page_id)
    return str(parent["spaceId"]) if live.platform == "cloud" else parent["space"]["key"]


def _parent_of(live, page_id: str) -> str:
    if live.platform == "cloud":
        return live.api.client.get_page_by_id(page_id)["parentId"]
    page = live.api.client.get_page_by_id(page_id, expand="ancestors")
    return page["ancestors"][-1]["id"]


def _status_of(exception: Exception) -> int | None:
    response = getattr(exception, "response", None)
    return None if response is None else response.status_code
