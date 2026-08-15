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

    def create(self, items, **kwargs):
        """Create a conference at the provider.

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
