"""
.. module: dispatch.plugins.dispatch_zoom.plugin
    :platform: Unix
    :copyright: (c) 2019 by HashCorp Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Will Bengtson <wbengtson@hashicorp.com>
"""

import logging
import random
from urllib.parse import quote

import requests

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import ConferenceCreatedButUnusable, DispatchPluginException
from dispatch.plugins import dispatch_zoom as zoom_plugin
from dispatch.plugins.bases import ConferencePlugin

from .config import ZoomConfiguration
from .client import ZoomClient

log = logging.getLogger(__name__)


def _quote_path_component(value) -> str:
    """Percent-encode one dynamic Zoom API path segment.

    ``safe=""`` is deliberate: a bare ``quote()`` leaves ``/`` unescaped,
    which is exactly the character that lets a value walk out of its own
    path segment. Only ever call this on a single path component, never on
    a full URL or a query string.

    A component that is *exactly* ``.`` or ``..`` is refused rather than
    encoded: ``quote()`` can never escape ``.`` (Python's always-safe set --
    letters, digits, ``_.-~`` -- is only ever added to by ``safe``, never
    subtracted from), and hand-escaping it (``.`` -> ``%2E``) doesn't help
    either -- ``requests.utils.requote_uri``, which every ``PreparedRequest``
    runs through, decodes any percent-triplet of an unreserved character
    back to its literal form before the request line is built, precisely
    because ``.`` is unreserved. So a value of ``".."`` always reaches the
    wire as a literal, un-encoded dot-segment no matter how it was escaped
    going in. Neither Zoom's API docs nor `requests` documents whether the
    server also normalises it away, so this codebase cannot prove it is
    safe to send -- it is refused instead.
    """
    value = str(value)
    if value in (".", ".."):
        raise DispatchPluginException(
            f"Refusing to build a Zoom API request with a path component of "
            f"{value!r}: it cannot be percent-encoded in a way that survives "
            f"requests' own URL normalization."
        )
    return quote(value, safe="")


def gen_conference_challenge(length: int):
    """Generate a random challenge for Zoom."""
    if length > 10:
        length = 10
    field = "abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.sample(field, length))


def delete_meeting(client, event_id: int):
    path = "meetings/{}".format(_quote_path_component(event_id))
    return request(client, "delete", path, "deletion of the meeting")


def check(response, operation: str):
    """Raise unless Zoom accepted the request.

    Every call goes through here. Scopes are not validated when the token is
    issued, so an under-scoped app authenticates and then fails per operation --
    without this, `create` would read Zoom's error body through `.get(key,
    default)` and hand back a conference that does not exist.
    """
    if 200 <= response.status_code < 300:
        return response

    # Only Zoom's structured `message` is repeated. The raw body is never
    # echoed: this request carries a live bearer token, and an intermediary
    # answering in Zoom's place may quote the request -- headers included --
    # back at us. This string reaches the incident timeline, which is broadly
    # readable and exportable.
    try:
        detail = response.json()["message"]
    except (ValueError, KeyError, TypeError):
        detail = "no reason given"

    raise DispatchPluginException(
        f"Zoom {operation} failed with HTTP {response.status_code}: {detail}"
    )


def request(client, method: str, path: str, operation: str, **kwargs):
    """Issue one Zoom call, turning transport failures into plugin exceptions."""
    try:
        response = getattr(client, method)(path, **kwargs)
    except requests.RequestException as e:
        raise DispatchPluginException(f"Zoom {operation} could not be completed: {e}") from e

    return check(response, operation)


def meeting_invitees(meeting: dict) -> list[dict]:
    """The meeting's invitees, for a meeting that may carry none.

    Zoom sends an explicit null rather than omitting the key, so
    `.get(key, [])` returns the null instead of the default.
    """
    settings = meeting.get("settings") or {}
    return list(settings.get("meeting_invitees") or [])


def as_invitee(participant: str) -> dict:
    """One participant as Zoom's invitee list represents them.

    Shared by `create` and `add_participant` so the two never disagree about what
    an invitee looks like -- they write to the same list, and Zoom replaces it
    wholesale.
    """
    return {"email": participant}


def invitee_matches(invitee: dict, participant: str) -> bool:
    """Whether an invitee is the given participant. Email is case-insensitive."""
    email = invitee.get("email")
    return bool(email) and email.casefold() == participant.casefold()


def get_meeting(client, event_id: str) -> dict:
    path = "meetings/{}".format(_quote_path_component(event_id))
    response = request(client, "get", path, "read of the meeting")
    try:
        return response.json()
    except ValueError as e:
        raise DispatchPluginException(
            f"Zoom returned HTTP {response.status_code} with a body that is not JSON."
        ) from e


def update_invitees(client, event_id: str, invitees: list[dict]):
    """Replace the meeting's invitee list.

    Only `meeting_invitees` is sent. Echoing back the whole settings object read
    a moment earlier would revert anything changed in the Zoom UI in between.
    """
    return request(
        client,
        "patch",
        "meetings/{}".format(_quote_path_component(event_id)),
        "update of the meeting invitees",
        data={"settings": {"meeting_invitees": invitees}},
    )


def create_meeting(
    client,
    user_id: str,
    name: str,
    description: str = None,
    title: str = None,
    participants: list[str] = None,
    duration: int = 1440,
):
    """Create a Zoom Meeting.

    Zoom caps `duration` at 1440 minutes (24 hours); the previous default of
    60000 claimed six weeks and was rejected by the API.

    `participants` seeds `settings.meeting_invitees`, the same list
    `add_participant` maintains (issue #110). The key is omitted entirely rather
    than sent empty when there is no one to list, which keeps the request
    identical to what a deployment with no bridge participants sends today.

    Zoom does not email an invitee. Its staff's answer is *"That value is just a
    list of the meeting's invitees. It does not generate an email, though all
    participants are emailed"* -- quoted whole because the trailing clause is the
    only ambiguous part, and it is read as referring to the separate registrants
    API the same reply points at, which this plugin never calls. Multiple later
    staff replies say plainly that the field triggers no email.

    **Unverified, and it bounds what this is worth:** staff have also said the
    field is consumed only by their calendar integrations, and that invitees may
    not be readable back at all. If a `GET` does not echo them, the roster is
    write-only and the first `add_participant` replaces it -- see
    `test_a_seeded_roster_zoom_does_not_echo_back_is_lost_on_the_first_add`. The
    request this builds is what the API documents; only the live suite can say
    what Zoom does with it.

    The invitees ride along in the create rather than being PATCHed on
    afterwards, so a list Zoom rejects fails while there is still no meeting to
    strand (issue #114). Nothing is truncated: Zoom documents no invitee cap, and
    a silently shortened roster is worse than a create that says why it failed.
    """
    body = {
        "topic": title if title else f"Situation Room for {name}",
        "agenda": description if description else f"Situation Room for {name}. Please join.",
        "duration": duration,
        "password": gen_conference_challenge(8),
        "settings": {"join_before_host": True},
    }

    if participants:
        body["settings"]["meeting_invitees"] = [as_invitee(p) for p in participants]

    return request(
        client,
        "post",
        "users/{}/meetings".format(_quote_path_component(user_id)),
        "creation of the meeting",
        data=body,
    )


@apply(timer, exclude=["__init__", "_zoom_client"])
@apply(counter, exclude=["__init__", "_zoom_client"])
class ZoomConferencePlugin(ConferencePlugin):
    title = "Zoom Plugin - Conference Management"
    slug = "zoom-conference"
    description = "Uses Zoom to manage conference meetings."
    version = zoom_plugin.__version__

    author = "HashCorp"
    author_url = "https://github.com/netflix/dispatch.git"

    def __init__(self):
        self.configuration_schema = ZoomConfiguration

    def _zoom_client(self) -> ZoomClient:
        # `PluginInstance.configuration` returns None when the stored JSON does
        # not satisfy the schema, logging a warning and nothing else. A config
        # written before the OAuth migration always lands here, so say what to
        # do -- this message is what reaches the incident timeline.
        if self.configuration is None:
            raise DispatchPluginException(
                "The Zoom plugin configuration could not be read. It may still hold the "
                "retired API Key and API Secret; re-enter the Server-to-Server OAuth "
                "credentials under Settings > Project > Plugins."
            )

        return ZoomClient(
            account_id=self.configuration.account_id,
            client_id=self.configuration.client_id,
            client_secret=self.configuration.client_secret.get_secret_value(),
        )

    def create(
        self, name: str, description: str = None, title: str = None, participants: list[str] = None
    ):
        """Create a new event.

        `participants` becomes the meeting's initial invitee roster. Roster
        metadata only -- the join_url works for anyone holding it whatever the
        list says.
        """
        client = self._zoom_client()

        conference_response = create_meeting(
            client,
            self.configuration.api_user_id,
            name,
            description=description,
            title=title,
            participants=participants,
            duration=self.configuration.default_duration_minutes,
        )

        # Zoom has committed the meeting by the time it answers 2xx, so every
        # rejection below strands a live bridge. They all raise
        # `ConferenceCreatedButUnusable`, which is what tells `create_conference`
        # to delete it -- carrying the id when the response yielded one, and
        # None when it did not, which still gets the possible leak logged rather
        # than filed under "the provider was down" (issue #114).
        try:
            conference_json = conference_response.json()
        except ValueError as e:
            raise ConferenceCreatedButUnusable(
                "Zoom accepted the meeting creation but returned a body that is not JSON."
            ) from e

        if not isinstance(conference_json, dict):
            # Otherwise `.get` below is an AttributeError, which reads as a
            # Dispatch bug rather than as a meeting Zoom is still holding.
            raise ConferenceCreatedButUnusable(
                f"Zoom accepted the meeting creation but returned a "
                f"{type(conference_json).__name__} where an object was expected."
            )

        # Deliberately not `.get(key, default)`. Defaulting here is how an error
        # response became a conference pointing at zoom.us with id "1".
        #
        # `password` is not required: an account whose policy disables meeting
        # passcodes returns a perfectly good join_url without one, and an empty
        # challenge is representable downstream. Teams behaves the same way.
        missing = [k for k in ("join_url", "id") if not conference_json.get(k)]
        if missing:
            meeting_id = conference_json.get("id")
            raise ConferenceCreatedButUnusable(
                f"Zoom created the meeting but omitted {', '.join(missing)}.",
                # Stringified to match what a successful create returns, since
                # Zoom sends the id as a JSON number. Never a fallback to
                # join_url or topic: those are not unique and could name another
                # incident's meeting.
                resource_id=str(meeting_id) if meeting_id else None,
            )

        return {
            "weblink": conference_json["join_url"],
            # Zoom sends the meeting id as a JSON number. `ConferenceCreate`
            # types both resource_id and conference_id as str, and pydantic v2
            # does not coerce int to str -- passing it through raises outside
            # the caller's try/except and aborts the whole incident flow.
            "id": str(conference_json["id"]),
            "challenge": conference_json.get("password") or "",
        }

    def delete(self, event_id: str):
        """Deletes an existing event."""
        delete_meeting(self._zoom_client(), event_id)

    def add_participant(self, event_id: str, participant: str):
        """Add a participant to the meeting's invitee roster.

        Roster metadata only: the join link works without this and this grants no
        access. Zoom replaces `meeting_invitees` wholesale, so the current list is
        read first and resent in full.

        Zoom support has stated that `meeting_invitees` is consumed only by their
        calendar integrations, so the invitee may never surface in the Zoom
        client. This sends what the API documents.

        Resending in full only preserves the existing roster if Zoom reports it on
        a read, which is unconfirmed and which staff have suggested it does not --
        if it does not, this replaces whatever `create` seeded (issue #110). The
        write-gated live tests are the only thing that can settle it.
        """
        client = self._zoom_client()
        invitees = meeting_invitees(get_meeting(client, event_id))

        if any(invitee_matches(i, participant) for i in invitees):
            return

        update_invitees(client, event_id, invitees + [as_invitee(participant)])

    def remove_participant(self, event_id: str, participant: str):
        """Remove a participant from the meeting's invitee roster.

        This evicts nobody and revokes no link; it only updates the roster. An
        absent participant is left alone rather than treated as an error, so a
        retried removal is a no-op.
        """
        client = self._zoom_client()
        invitees = meeting_invitees(get_meeting(client, event_id))

        remaining = [i for i in invitees if not invitee_matches(i, participant)]
        if len(remaining) == len(invitees):
            return

        update_invitees(client, event_id, remaining)
