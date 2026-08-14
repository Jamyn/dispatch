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
    return client.delete("/meetings/{}".format(event_id))


def check(response, operation: str):
    """Raise unless Zoom accepted the request.

    The pre-existing create/delete calls ignore the status entirely; the
    participant operations cannot, because a failed read followed by a write
    would replace the invitee list with a truncated one.
    """
    if 200 <= response.status_code < 300:
        return response

    try:
        detail = response.json()["message"]
    except (ValueError, KeyError, TypeError):
        detail = response.text.strip()[:200] or "no response body"

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
    duration: int = 60000,  # duration in mins ~6 weeks
):
    """Create a Zoom Meeting."""
    body = {
        "topic": title if title else f"Situation Room for {name}",
        "agenda": description if description else f"Situation Room for {name}. Please join.",
        "duration": duration,
        "password": gen_conference_challenge(8),
        "settings": {"join_before_host": True},
    }

    return client.post("/users/{}/meetings".format(user_id), data=body)


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
        return ZoomClient(
            self.configuration.api_key, self.configuration.api_secret.get_secret_value()
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

        conference_json = conference_response.json()

        return {
            "weblink": conference_json.get("join_url", "https://zoom.us"),
            "id": conference_json.get("id", "1"),
            "challenge": conference_json.get("password", "123"),
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
        client. This sends what the API documents; issue #70 (the retired JWT auth
        this plugin still uses) blocks confirming the behaviour against a real
        account.
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
