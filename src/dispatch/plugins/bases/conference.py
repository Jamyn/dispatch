"""
.. module: dispatch.plugins.bases.conference
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Kevin Glisson <kglisson@netflix.com>
"""

from dispatch.plugins.base import Plugin


class ConferencePlugin(Plugin):
    type = "conference"

    def create(
        self,
        name: str,
        description: str = None,
        title: str = None,
        participants: list[str] = None,
        **kwargs,
    ):
        """Create a conference at the provider.

        `participants` is the conference's initial roster: the email addresses to
        list on the meeting as it is created. Dispatch treats it as invitation
        metadata and nothing else -- it gates no join link, grants no access and
        revokes none, and no code here may assume otherwise (issue #110).

        That is a statement about Dispatch, not a guarantee about every provider.
        A provider may attach its own meaning to its roster -- Teams' lobby can be
        configured to admit only invited people (`lobbyBypassSettings.scope =
        invited`), which Dispatch neither sets nor relies on. So a roster entry is
        never the thing that lets someone in, but on some tenants its absence may
        still cost a responder a wait in the lobby. **Inferred**: whether a
        standalone onlineMeeting's attendee counts as "invited" for that purpose is
        not documented either way.

        `create_conference` normalises the list before calling this, so it arrives
        free of duplicates and in a stable order; an empty list means "create the
        conference with no roster" and must succeed.

        Apply it within the provider's create call rather than adding the roster
        afterwards. A follow-up write opens exactly the window issue #114 exists to
        close: the meeting is committed, the roster call fails, and the id is lost.
        Inside the create, a roster the provider rejects fails before it commits
        anything.

        Must raise `ConferenceCreatedButUnusable` -- not `DispatchPluginException`
        -- for any failure that happens *after* the provider accepted the
        meeting, passing the provider's id when the response carried one. That
        exception is the only signal `create_conference` has that a meeting was
        created and needs deleting; anything else is read as "the provider gave
        us nothing" and leaves a live bridge with no database row behind it
        (issue #114).
        """
        raise NotImplementedError

    def delete(self, event_id: str):
        """Delete the conference with the provider's id.

        Declared so the requirement is discoverable: both `delete_conference`
        (incident teardown) and `create_conference`'s compensating cleanup call
        this on whichever conference plugin is enabled.
        """
        raise NotImplementedError
