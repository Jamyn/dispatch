"""Security regression tests for issue #123: path-component encoding in the
Microsoft Graph client.

``MSTeamsClient`` used to interpolate ``user_id`` and ``meeting_id`` into
request paths with an f-string. ``requests`` normalises dot segments in a URL
path before it ever reaches the wire, so a value containing one could retarget
the request at an entirely different Graph resource:

    .../onlineMeetings/../../me/photo  ->  https://graph.microsoft.com/v1.0/me/photo

Every test below inspects the *actual* outgoing request path via
``graph_capture`` (which bypasses ``FakeGraph``'s strict route matching so a
retargeted request is observable instead of raising "unexpected request").
None of them assert that ``quote`` was called -- they assert what URL the
transport actually received.

The parametrized tests check an encoding-scheme-independent invariant rather
than a hardcoded expected string: each dynamic value must occupy exactly one
path segment, and percent-decoding that segment must recover the original
value exactly. A hardcoded ``quote(value, safe="")`` expectation would have
been *wrong* for a value of ``".."`` -- `quote()` can never escape a dot, so
the correct fix for that case percent-encodes the dots themselves (``%2E%2E``
instead of ``..``), which the segment/round-trip invariant validates without
assuming which escaping strategy was used.
"""

from urllib.parse import unquote

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import MEETING_ID, USER_ID
from tests.plugins.dispatch_microsoft_teams.test_client import build_client

BASE_PATH = "/v1.0"

# One of each URL-significant character class called out in issue #123, plus
# a concrete path-traversal payload from its own threat model. A bare "." or
# ".." is deliberately NOT in this list -- see REJECTED_PATH_COMPONENTS below,
# they need a different fix and a different assertion.
MALICIOUS_PATH_COMPONENTS = [
    "../me/photo",
    "../../me/photo",
    "foo/bar",
    "foo?bar",
    "foo#bar",
    "foo%2Fbar",
    "foo+bar",
    "foo=bar",
    "foo@bar",
    "foo*bar",
]

# A path component that is *exactly* "." or ".." cannot be made safe by
# percent-encoding at all, so the client must refuse to build a request with
# one rather than send something whose safety it cannot prove:
#
# `quote(value, safe="")` can never escape "." -- Python's always-safe set
# (letters, digits, `_.-~`) is only ever added to by `safe`, never subtracted
# from. Escaping the dots by hand (`.` -> `%2E`) doesn't help either: verified
# empirically that `requests.utils.requote_uri` -- which every
# `PreparedRequest` runs through -- decodes any percent-triplet of an
# unreserved character back to its literal form before building the request
# line (`%2E` -> `.`), specifically because `.` is unreserved. So a value of
# exactly ".." always reaches the wire as a literal, un-encoded ".." path
# segment no matter how it was escaped going in; whether the receiving server
# then also applies RFC 3986 dot-segment normalization to it is Microsoft's
# implementation detail, not something this codebase can verify or rely on.
REJECTED_PATH_COMPONENTS = [".", ".."]

# Ordinary values that must keep working: no URL-significant characters, so
# encoding is a no-op and the segment is byte-identical to the raw value.
ORDINARY_IDS = [
    "user@example.com",
    "user-id",
    "meeting-id",
    "123456",
    MEETING_ID,
]


def assert_create_path(path: str, user_id: str) -> None:
    """``user_id`` must be exactly one path segment, recoverable by unquote."""
    parts = path.split("/")
    assert len(parts) == 5, f"user_id did not stay a single path segment: {path}"
    assert parts[:3] == ["", "v1.0", "users"], path
    assert parts[4] == "onlineMeetings", path
    assert unquote(parts[3]) == user_id, f"user_id was not recoverable from {path}"


def assert_meeting_path(path: str, user_id: str, meeting_id: str) -> None:
    """Both ids must be exactly one path segment each, recoverable by unquote."""
    parts = path.split("/")
    assert len(parts) == 6, f"user_id/meeting_id did not stay single path segments: {path}"
    assert parts[:3] == ["", "v1.0", "users"], path
    assert parts[4] == "onlineMeetings", path
    assert unquote(parts[3]) == user_id, f"user_id was not recoverable from {path}"
    assert unquote(parts[5]) == meeting_id, f"meeting_id was not recoverable from {path}"


# --- create: only user_id is a dynamic path component -----------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_create_meeting_encodes_a_malicious_user_id(graph_capture, malicious):
    build_client(user_id=malicious).create_meeting(subject="Situation Room")

    request = graph_capture[-1]
    assert request.method == "POST"
    assert_create_path(request.path, malicious)


@pytest.mark.parametrize("user_id", ORDINARY_IDS)
def test_create_meeting_leaves_an_ordinary_user_id_readable(graph_capture, user_id):
    build_client(user_id=user_id).create_meeting(subject="Situation Room")

    assert_create_path(graph_capture[-1].path, user_id)


# --- delete -------------------------------------------------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_delete_meeting_encodes_a_malicious_meeting_id(graph_capture, malicious):
    build_client().delete_meeting(malicious)

    request = graph_capture[-1]
    assert request.method == "DELETE"
    assert_meeting_path(request.path, USER_ID, malicious)


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_delete_meeting_encodes_a_malicious_user_id(graph_capture, malicious):
    build_client(user_id=malicious).delete_meeting(MEETING_ID)

    assert_meeting_path(graph_capture[-1].path, malicious, MEETING_ID)


@pytest.mark.parametrize("meeting_id", ORDINARY_IDS)
def test_delete_meeting_leaves_an_ordinary_meeting_id_readable(graph_capture, meeting_id):
    build_client().delete_meeting(meeting_id)

    assert_meeting_path(graph_capture[-1].path, USER_ID, meeting_id)


# --- get ------------------------------------------------------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_get_meeting_encodes_a_malicious_meeting_id(graph_capture, malicious):
    build_client().get_meeting(malicious)

    request = graph_capture[-1]
    assert request.method == "GET"
    assert_meeting_path(request.path, USER_ID, malicious)


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_get_meeting_encodes_a_malicious_user_id(graph_capture, malicious):
    build_client(user_id=malicious).get_meeting(MEETING_ID)

    assert_meeting_path(graph_capture[-1].path, malicious, MEETING_ID)


@pytest.mark.parametrize("meeting_id", ORDINARY_IDS)
def test_get_meeting_leaves_an_ordinary_meeting_id_readable(graph_capture, meeting_id):
    build_client().get_meeting(meeting_id)

    assert_meeting_path(graph_capture[-1].path, USER_ID, meeting_id)


# --- update attendees ---------------------------------------------------


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_update_attendees_encodes_a_malicious_meeting_id(graph_capture, malicious):
    build_client().update_attendees(malicious, [{"upn": "responder@example.com"}])

    request = graph_capture[-1]
    assert request.method == "PATCH"
    assert_meeting_path(request.path, USER_ID, malicious)


@pytest.mark.parametrize("malicious", MALICIOUS_PATH_COMPONENTS)
def test_update_attendees_encodes_a_malicious_user_id(graph_capture, malicious):
    build_client(user_id=malicious).update_attendees(MEETING_ID, [{"upn": "responder@example.com"}])

    assert_meeting_path(graph_capture[-1].path, malicious, MEETING_ID)


@pytest.mark.parametrize("meeting_id", ORDINARY_IDS)
def test_update_attendees_leaves_an_ordinary_meeting_id_readable(graph_capture, meeting_id):
    build_client().update_attendees(meeting_id, [{"upn": "responder@example.com"}])

    assert_meeting_path(graph_capture[-1].path, USER_ID, meeting_id)


# --- the threat model, made explicit -----------------------------------


def test_a_traversal_user_id_cannot_retarget_the_request_to_me(graph_capture):
    """The exact scenario from issue #123: a traversal user_id must not make
    the request land on the operator's own ``/me`` resource."""
    build_client(user_id="../me").get_meeting("photo")

    request = graph_capture[-1]
    assert not request.path.startswith("/v1.0/me/"), (
        f"traversal user_id retargeted the request: {request.path}"
    )
    assert_meeting_path(request.path, "../me", "photo")


def test_a_traversal_meeting_id_stays_a_single_path_component(graph_capture):
    build_client().get_meeting("../../me/photo")

    request = graph_capture[-1]
    assert "onlineMeetings" in request.path, (
        f"traversal meeting_id dropped the onlineMeetings segment: {request.path}"
    )
    assert_meeting_path(request.path, USER_ID, "../../me/photo")


@pytest.mark.parametrize("dotted", REJECTED_PATH_COMPONENTS)
def test_a_bare_dot_segment_user_id_is_refused_rather_than_sent(graph_capture, dotted):
    """No encoding of a bare "." or ".." survives `requests`' own URL
    canonization (see REJECTED_PATH_COMPONENTS), so the client must refuse to
    build the request at all rather than gamble on server-side behavior it
    cannot verify."""
    with pytest.raises(DispatchPluginException):
        build_client(user_id=dotted).get_meeting(MEETING_ID)

    assert graph_capture == [], "a request was sent despite the dot-only id"


@pytest.mark.parametrize("dotted", REJECTED_PATH_COMPONENTS)
def test_a_bare_dot_segment_meeting_id_is_refused_rather_than_sent(graph_capture, dotted):
    with pytest.raises(DispatchPluginException):
        build_client().get_meeting(dotted)

    assert graph_capture == [], "a request was sent despite the dot-only id"


@pytest.mark.parametrize("dotted", REJECTED_PATH_COMPONENTS)
def test_a_bare_dot_segment_is_refused_for_every_operation(graph_capture, dotted):
    for call in (
        lambda: build_client(user_id=dotted).create_meeting(subject="x"),
        lambda: build_client().delete_meeting(dotted),
        lambda: build_client().get_meeting(dotted),
        lambda: build_client().update_attendees(dotted, [{"upn": "r@example.com"}]),
    ):
        with pytest.raises(DispatchPluginException):
            call()

    assert graph_capture == [], "a request was sent despite a dot-only id"


# --- no double encoding --------------------------------------------------


def test_a_value_that_already_looks_percent_encoded_is_not_decoded_first(graph_capture):
    """A literal ``%2F`` in the raw id must round-trip back to ``foo%2Fbar``,
    not ``foo/bar`` -- proving the value is quoted once, not decoded first."""
    build_client().get_meeting("foo%2Fbar")

    assert_meeting_path(graph_capture[-1].path, USER_ID, "foo%2Fbar")
