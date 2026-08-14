"""Microsoft Teams conference plugin."""

import logging

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import DispatchPluginException
from dispatch.plugins.bases import ConferencePlugin
from dispatch.plugins.dispatch_microsoft_teams import conference as teams_plugin

from .client import MSTeamsClient
from .config import MicrosoftTeamsConfiguration

log = logging.getLogger(__name__)


def build_challenge(meeting: dict) -> str:
    """Render the passcode with the meeting id it is used with.

    The passcode only applies to joining by meeting id -- the join link carries
    its own authentication context -- so the passcode alone would be a
    credential with nothing to enter it into.

    Module level rather than a static method because `apply` rewrites every
    callable in cls.__dict__ and turns a staticmethod into a bound method.
    """
    # `.get(key, default)` returns the default only when the key is absent;
    # Graph sends an explicit null when there are no passcode settings.
    settings = meeting.get("joinMeetingIdSettings") or {}
    passcode = settings.get("passcode")
    if not passcode:
        return ""

    meeting_id = settings.get("joinMeetingId")
    return f"{passcode} (meeting ID {meeting_id})" if meeting_id else passcode


# `apply` rewrites the entries of cls.__dict__, so it must decorate the class.
# Decorating a method instead is a silent no-op -- a function's __dict__ is
# empty, so the loop body never runs and no metric is ever emitted.
@apply(timer, exclude=["__init__", "_client"])
@apply(counter, exclude=["__init__", "_client"])
class MicrosoftTeamsConferencePlugin(ConferencePlugin):
    title = "Microsoft Teams Plugin - Conference Management"
    slug = "microsoft-teams-conference"
    description = "Uses MS Teams to manage conference meetings."
    version = teams_plugin.__version__

    author = "Cino Jose"
    author_url = "https://github.com/netflix/dispatch.git"

    def __init__(self):
        self.configuration_schema = MicrosoftTeamsConfiguration

    def _client(self) -> MSTeamsClient:
        return MSTeamsClient(
            client_id=self.configuration.client_id,
            authority=self.configuration.authority,
            credential=self.configuration.secret.get_secret_value(),
            user_id=self.configuration.user_id,
        )

    def create(
        self, name: str, description: str = None, title: str = None, participants: list[str] = None
    ):
        """Create a new event.

        `description` and `participants` are accepted for interface parity and
        unused: an onlineMeeting has no agenda field, and responders join
        through the link Dispatch publishes rather than being invited.
        """
        meeting = self._client().create_meeting(
            subject=title if title else f"Situation Room for {name}",
            duration_minutes=self.configuration.default_duration_minutes,
            record_automatically=self.configuration.allow_auto_recording,
            require_passcode=self.configuration.require_passcode,
        )

        # conference/flows.py subscripts these without a default, so an
        # incomplete meeting has to fail here rather than downstream. The body
        # itself is deliberately not quoted -- it carries the passcode and the
        # dial-in conference id, and this message reaches the incident timeline.
        for field in ("joinWebUrl", "id"):
            if not meeting.get(field):
                raise DispatchPluginException(
                    f"Microsoft Graph created a meeting without a {field}."
                )

        return {
            "weblink": meeting["joinWebUrl"],
            "id": meeting["id"],
            "challenge": build_challenge(meeting),
        }

    def delete(self, event_id: str):
        """Deletes an existing event."""
        self._client().delete_meeting(event_id)

    def add_participant(self, event_id: str, participant: str):
        """Adds a new participant to event."""
        return

    def remove_participant(self, event_id: str, participant: str):
        """Removes a participant from event."""
        return
