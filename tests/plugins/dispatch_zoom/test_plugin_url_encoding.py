"""Security regression tests for the Zoom counterpart of issue #123:
path-component encoding for Zoom API requests.

``dispatch_zoom.plugin`` used to ``.format()`` ``user_id``/``event_id``
straight into Zoom API request paths (``create_meeting``, ``delete_meeting``,
``get_meeting``, ``update_invitees``). ``requests`` normalises dot segments in
a URL path before it ever reaches the wire, so a value containing one could
retarget the request at an entirely different Zoom resource:

    .../meetings/../../me  ->  https://api.zoom.us/me

Every test below inspects the *actual* outgoing request URL via the existing
``zoom`` fixture (``FakeZoom`` already falls through to a generic response for
any unmatched path rather than raising, so a retargeted request is directly
observable -- no extra fixture is needed here, unlike the Teams suite).

The parametrized tests check an encoding-scheme-independent invariant rather
than a hardcoded expected string: each dynamic value must occupy exactly one
path segment, and percent-decoding that segment must recover the original
value exactly. A bare "." or ".." cannot be made safe by percent-encoding at
all (see ``_quote_path_component``'s docstring), so those are tested
separately for outright rejection.
"""

from urllib.parse import unquote, urlparse

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_zoom.conftest import MEETING_ID
from tests.plugins.dispatch_zoom.test_zoom_oauth import build_client

API_BASE = "/v2"

# One of each URL-significant character class called out in issue #123, plus
# a concrete path-traversal payload from its own threat model. A bare "." or
# ".." is deliberately NOT in this list -- see REJECTED_PATH_COMPONENTS.
MALICIOUS_PATH_COMPONENTS = [
    "../me/photo",
    "../../me",
    "foo/bar",
    "foo?bar",
    "foo#bar",
    "foo%2Fbar",
    "foo+bar",
    "foo=bar",
    "foo@bar",
    "foo*bar",
]

# See `_quote_path_component` in dispatch_zoom/plugin.py: `quote()` can never
# escape "." (Python's always-safe set -- letters, digits, `_.-~` -- is only
# ever added to by `safe`, never subtracted from), and hand-escaping the dots
# (`.` -> `%2E`) doesn't survive `requests.utils.requote_uri` either, which
# decodes any percent-triplet of an unreserved character back to its literal
# form before the request line is built. So these two values are refused
# outright rather than sent.
REJECTED_PATH_COMPONENTS = [".", ".."]

ORDINARY_IDS = ["responder@example.com", "user-id", "123456", MEETING_ID]


def path_of(url: str) -> str:
    return urlparse(url).path


def assert_create_path(path: str, user_id: str) -> None:
    parts = path.split("/")
    assert len(parts) == 5, f"user_id did not stay a single path segment: {path}"
    assert parts[:3] == ["", "v2", "users"], path
    assert parts[4] == "meetings", path
    assert unquote(parts[3]) == user_id, f"user_id was not recoverable from {path}"


def assert_meeting_path(path: str, event_id: str) -> None:
    parts = path.split("/")
    assert len(parts) == 4, f"event_id did not stay a single path segment: {path}"
    assert parts[:3] == ["", "v2", "meetings"], path
    assert unquote(parts[3]) == event_id, f"event_id was not recoverable from {path}"


# --- create: only user_id is a dynamic path component -----------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_create_meeting_encodes_a_malicious_user_id(zoom, malicious):
    from dispatch.plugins.dispatch_zoom.plugin import create_meeting

    create_meeting(build_client(), malicious, "Situation Room")

    request = zoom.last_api_request()
    assert request.method == "POST"
    assert_create_path(path_of(request.url), malicious)


@pytest.mark.parametrize("user_id", ORDINARY_IDS)
def test_create_meeting_leaves_an_ordinary_user_id_readable(zoom, user_id):
    from dispatch.plugins.dispatch_zoom.plugin import create_meeting

    create_meeting(build_client(), user_id, "Situation Room")

    assert_create_path(path_of(zoom.last_api_request().url), user_id)


# --- delete -------------------------------------------------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_delete_meeting_encodes_a_malicious_event_id(zoom, malicious):
    from dispatch.plugins.dispatch_zoom.plugin import delete_meeting

    delete_meeting(build_client(), malicious)

    request = zoom.last_api_request()
    assert request.method == "DELETE"
    assert_meeting_path(path_of(request.url), malicious)


@pytest.mark.parametrize("event_id", ORDINARY_IDS)
def test_delete_meeting_leaves_an_ordinary_event_id_readable(zoom, event_id):
    from dispatch.plugins.dispatch_zoom.plugin import delete_meeting

    delete_meeting(build_client(), event_id)

    assert_meeting_path(path_of(zoom.last_api_request().url), event_id)


# --- get ------------------------------------------------------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_get_meeting_encodes_a_malicious_event_id(zoom, malicious):
    from dispatch.plugins.dispatch_zoom.plugin import get_meeting

    get_meeting(build_client(), malicious)

    request = zoom.last_api_request()
    assert request.method == "GET"
    assert_meeting_path(path_of(request.url), malicious)


@pytest.mark.parametrize("event_id", ORDINARY_IDS)
def test_get_meeting_leaves_an_ordinary_event_id_readable(zoom, event_id):
    from dispatch.plugins.dispatch_zoom.plugin import get_meeting

    get_meeting(build_client(), event_id)

    assert_meeting_path(path_of(zoom.last_api_request().url), event_id)


# --- update invitees ---------------------------------------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_update_invitees_encodes_a_malicious_event_id(zoom, malicious):
    from dispatch.plugins.dispatch_zoom.plugin import update_invitees

    update_invitees(build_client(), malicious, [{"email": "responder@example.com"}])

    request = zoom.last_api_request()
    assert request.method == "PATCH"
    assert_meeting_path(path_of(request.url), malicious)


@pytest.mark.parametrize("event_id", ORDINARY_IDS)
def test_update_invitees_leaves_an_ordinary_event_id_readable(zoom, event_id):
    from dispatch.plugins.dispatch_zoom.plugin import update_invitees

    update_invitees(build_client(), event_id, [{"email": "responder@example.com"}])

    assert_meeting_path(path_of(zoom.last_api_request().url), event_id)


# --- the threat model, made explicit -----------------------------------


def test_a_traversal_event_id_cannot_retarget_the_request_to_me(zoom):
    """The Zoom analogue of issue #123's own example: a traversal event_id
    must not make the request land on a completely different resource."""
    from dispatch.plugins.dispatch_zoom.plugin import get_meeting

    get_meeting(build_client(), "../../users/me")

    request = zoom.last_api_request()
    path = path_of(request.url)
    assert not path.startswith("/v2/users/"), f"traversal event_id retargeted the request: {path}"
    assert_meeting_path(path, "../../users/me")


def test_a_traversal_user_id_cannot_retarget_a_create(zoom):
    from dispatch.plugins.dispatch_zoom.plugin import create_meeting

    create_meeting(build_client(), "../../users/me", "Situation Room")

    request = zoom.last_api_request()
    path = path_of(request.url)
    assert not path.startswith("/v2/users/me"), f"traversal user_id retargeted the request: {path}"
    assert_create_path(path, "../../users/me")


# --- bare dot-segments: refused, not encoded -----------------------------


@pytest.mark.parametrize("dotted", REJECTED_PATH_COMPONENTS)
def test_a_bare_dot_segment_event_id_is_refused_rather_than_sent(zoom, dotted):
    from dispatch.plugins.dispatch_zoom.plugin import get_meeting

    with pytest.raises(DispatchPluginException):
        get_meeting(build_client(), dotted)

    assert zoom.api_requests() == [], "a request was sent despite the dot-only id"


@pytest.mark.parametrize("dotted", REJECTED_PATH_COMPONENTS)
def test_a_bare_dot_segment_user_id_is_refused_rather_than_sent(zoom, dotted):
    from dispatch.plugins.dispatch_zoom.plugin import create_meeting

    with pytest.raises(DispatchPluginException):
        create_meeting(build_client(), dotted, "Situation Room")

    assert zoom.api_requests() == [], "a request was sent despite the dot-only id"


@pytest.mark.parametrize("dotted", REJECTED_PATH_COMPONENTS)
def test_a_bare_dot_segment_is_refused_for_every_operation(zoom, dotted):
    from dispatch.plugins.dispatch_zoom.plugin import (
        create_meeting,
        delete_meeting,
        get_meeting,
        update_invitees,
    )

    for call in (
        lambda: create_meeting(build_client(), dotted, "Situation Room"),
        lambda: delete_meeting(build_client(), dotted),
        lambda: get_meeting(build_client(), dotted),
        lambda: update_invitees(build_client(), dotted, [{"email": "r@example.com"}]),
    ):
        with pytest.raises(DispatchPluginException):
            call()

    assert zoom.api_requests() == [], "a request was sent despite a dot-only id"


# --- no double encoding --------------------------------------------------


def test_a_value_that_already_looks_percent_encoded_is_not_decoded_first(zoom):
    """A literal ``%2F`` in the raw id must round-trip back to ``foo%2Fbar``,
    not ``foo/bar`` -- proving the value is quoted once, not decoded first."""
    from dispatch.plugins.dispatch_zoom.plugin import get_meeting

    get_meeting(build_client(), "foo%2Fbar")

    assert_meeting_path(path_of(zoom.last_api_request().url), "foo%2Fbar")
