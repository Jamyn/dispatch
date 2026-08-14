"""
.. module: dispatch.plugins.dispatch_microsoft_teams.conference.plugin
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Cino Jose
"""

import logging

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import DispatchPluginException
from dispatch.plugins.bases import ConferencePlugin
from dispatch.plugins.dispatch_microsoft_teams import conference as teams_plugin

from .client import MSTeamsClient
from .config import MicrosoftTeamsConfiguration

log = logging.getLogger(__name__)


# `apply` rewrites the entries of cls.__dict__, so it must decorate the class.
# Decorating a method instead is a silent no-op -- a function's __dict__ is
# empty, so the loop body never runs and no metric is ever emitted.
@apply(timer, exclude=["__init__"])
@apply(counter, exclude=["__init__"])
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
            record_automatically=self.configuration.allow_auto_recording,
        )

    def create(
        self, name: str, description: str = None, title: str = None, participants: list[str] = None
    ):
        """Create a new event."""
        meeting = self._client().create_meeting(
            subject=title if title else f"Situation Room for {name}",
            duration_minutes=self.configuration.default_duration_minutes,
            require_passcode=self.configuration.require_passcode,
        )

        # conference/flows.py subscripts all three of these without a default,
        # so an incomplete meeting has to fail here rather than downstream.
        for field in ("joinWebUrl", "id"):
            if not meeting.get(field):
                raise DispatchPluginException(
                    f"Microsoft Graph created a meeting without a {field}: {meeting}"
                )

        return {
            "weblink": meeting["joinWebUrl"],
            "id": meeting["id"],
            "challenge": meeting.get("joinMeetingIdSettings", {}).get("passcode") or "",
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
