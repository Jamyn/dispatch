"""
.. module: dispatch.plugins.dispatch_zoom.plugin
    :platform: Unix
    :copyright: (c) 2019 by HashCorp Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Will Bengtson <wbengtson@hashicorp.com>
"""

import logging
import random

import requests

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import DispatchPluginException
from dispatch.plugins import dispatch_zoom as zoom_plugin
from dispatch.plugins.bases import ConferencePlugin

from .config import ZoomConfiguration
from .client import ZoomClient

log = logging.getLogger(__name__)


def gen_conference_challenge(length: int):
    """Generate a random challenge for Zoom."""
    if length > 10:
        length = 10
    field = "abcdefghijklmnopqrstuvwxyz01234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.sample(field, length))


def delete_meeting(client, event_id: int):
    return request(client, "delete", "meetings/{}".format(event_id), "deletion of the meeting")


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


def invitee_matches(invitee: dict, participant: str) -> bool:
    """Whether an invitee is the given participant. Email is case-insensitive."""
    email = invitee.get("email")
    return bool(email) and email.casefold() == participant.casefold()


def get_meeting(client, event_id: str) -> dict:
    response = request(client, "get", "meetings/{}".format(event_id), "read of the meeting")
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
        "meetings/{}".format(event_id),
        "update of the meeting invitees",
        data={"settings": {"meeting_invitees": invitees}},
    )


def create_meeting(
    client,
    user_id: str,
    name: str,
    description: str = None,
    title: str = None,
    duration: int = 1440,
):
    """Create a Zoom Meeting.

    Zoom caps `duration` at 1440 minutes (24 hours); the previous default of
    60000 claimed six weeks and was rejected by the API.
    """
    body = {
        "topic": title if title else f"Situation Room for {name}",
        "agenda": description if description else f"Situation Room for {name}. Please join.",
        "duration": duration,
        "password": gen_conference_challenge(8),
        "settings": {"join_before_host": True},
    }

    return request(
        client,
        "post",
        "users/{}/meetings".format(user_id),
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
        """Create a new event."""
        client = self._zoom_client()

        conference_response = create_meeting(
            client,
            self.configuration.api_user_id,
            name,
            description=description,
            title=title,
            duration=self.configuration.default_duration_minutes,
        )

        try:
            conference_json = conference_response.json()
        except ValueError as e:
            raise DispatchPluginException(
                "Zoom accepted the meeting creation but returned a body that is not JSON."
            ) from e

        # Deliberately not `.get(key, default)`. Defaulting here is how an error
        # response became a conference pointing at zoom.us with id "1".
        #
        # `password` is not required: an account whose policy disables meeting
        # passcodes returns a perfectly good join_url without one, and an empty
        # challenge is representable downstream. Teams behaves the same way.
        missing = [k for k in ("join_url", "id") if not conference_json.get(k)]
        if missing:
            raise DispatchPluginException(
                f"Zoom created the meeting but omitted {', '.join(missing)}."
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
        client. This sends what the API documents; the live suite is read-only
        and creates no meeting, so that behaviour remains unconfirmed here.
        """
        client = self._zoom_client()
        invitees = meeting_invitees(get_meeting(client, event_id))

        if any(invitee_matches(i, participant) for i in invitees):
            return

        update_invitees(client, event_id, invitees + [{"email": participant}])

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
