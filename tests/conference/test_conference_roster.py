"""The initial conference roster, end to end through the flow (issue #110).

``incident_create_resources`` computed a roster, ``create_conference`` forwarded
it, and two of the three shipped plugins dropped it on the floor -- so the
"Add me automatically to incident bridges" switch decided nothing and the
database query behind it was pure waste.

The assertion that matters here is that the roster *arrives at the plugin*, which
is the layer that turns it into a provider request. What each provider then puts
on the wire is asserted against a fake transport in
``tests/plugins/dispatch_zoom/test_zoom_create_roster.py``,
``tests/plugins/dispatch_microsoft_teams/test_teams_create_roster.py`` and
``tests/plugins/dispatch_google_calendar/test_conference_plugin.py``.

Between those suites and this one the path is covered **as far as the outgoing
HTTP request**, on real payloads rather than on argument forwarding. It stops
there: whether a provider *stores* what it is sent can only be answered by the
live suites, which are gated on credentials that are unset locally and always
unset on forks. So a green run here proves Dispatch builds the right request, not
that a roster exists at the provider.

Roster membership is metadata. Nothing below claims an excluded address cannot
join the bridge -- every provider's join link works for whoever holds it.
"""

from types import SimpleNamespace

import pytest

from dispatch.conference.flows import create_conference, normalize_participants
from dispatch.incident.flows import filter_participants_for_bridge
from dispatch.plugins.base import plugins, register
from dispatch.plugins.bases import ConferencePlugin

from tests.factories import GroupFactory, PluginFactory, PluginInstanceFactory

MEETING_ID = "meeting-roster-1"
WEBLINK = "https://conference.example.invalid/j/meeting-roster-1"

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
CAROL = "carol@example.invalid"

# `.invalid` is safe while an address stays a plain string. The end-to-end test
# below uses `@example.com`, because there the address reaches a pydantic
# `EmailStr` and email-validator rejects RFC 2606 reserved TLDs.


class RosterRecordingPlugin(ConferencePlugin):
    """A registered conference plugin that keeps the roster it was created with.

    Resolved through the real registry rather than by patching
    ``PluginInstance.instance``, so the ORM dereference production performs runs
    here too.
    """

    title = "Dispatch Test Plugin - Conference Roster"
    slug = "test-conference-roster"
    description = "Records the initial roster the conference flow supplies."

    rosters = None

    def reset(self):
        self.rosters = []
        return self

    def create(self, name, description=None, title=None, participants=None):
        self.rosters.append(participants)
        return {"id": MEETING_ID, "weblink": WEBLINK, "challenge": ""}

    def delete(self, event_id):
        return


@pytest.fixture
def roster_plugin(session, incident):
    register(RosterRecordingPlugin)
    recorder = plugins.get(RosterRecordingPlugin.slug).reset()
    PluginInstanceFactory(
        project=incident.project,
        plugin=PluginFactory(type="conference", slug=RosterRecordingPlugin.slug),
        enabled=True,
    )
    yield recorder
    recorder.reset()


# --- the regression: the roster reaches the provider layer -------------------


def test_the_roster_reaches_the_plugin(session, incident, roster_plugin):
    """Pins the flow's half of the contract. The list always got this far -- it
    was the Zoom and Teams plugins that then dropped it, which is where the
    regression tests for issue #110 proper live. This one exists so a later
    refactor of ``create_conference`` cannot quietly stop supplying it.
    """
    create_conference(incident=incident, participants=[ALICE, BOB], db_session=session)

    assert roster_plugin.rosters == [[ALICE, BOB]]


def test_a_single_participant_reaches_the_plugin(session, incident, roster_plugin):
    create_conference(incident=incident, participants=[ALICE], db_session=session)

    assert roster_plugin.rosters == [[ALICE]]


def test_an_empty_roster_still_creates_the_conference(session, incident, roster_plugin):
    """Nobody opted in, or no participant plugin resolved anyone. The bridge is
    still created -- it just starts with nobody listed on it."""
    conference = create_conference(incident=incident, participants=[], db_session=session)

    assert conference is not None
    assert incident.conference is conference
    assert roster_plugin.rosters == [[]]


def test_no_roster_at_all_is_an_empty_roster(session, incident, roster_plugin):
    """``None`` and ``[]`` must not reach a plugin as two different things: one
    provider would omit the key and the other would send a null."""
    create_conference(incident=incident, participants=None, db_session=session)

    assert roster_plugin.rosters == [[]]


# --- normalization: what Dispatch guarantees the provider ---------------------


def test_duplicates_never_reach_the_provider(session, incident, roster_plugin):
    """Duplicates are reachable, not hypothetical: the participant resolver
    appends one entry for a direct individual match and another for a service
    whose on-call resolves to the same person, and nothing upstream collapses
    them."""
    create_conference(incident=incident, participants=[ALICE, BOB, ALICE], db_session=session)

    assert roster_plugin.rosters == [[ALICE, BOB]]


def test_duplicates_differing_only_in_case_never_reach_the_provider(
    session, incident, roster_plugin
):
    """Matched the same way every plugin's ``add_participant`` already matches, so
    one address is one roster entry whichever end writes it."""
    create_conference(incident=incident, participants=[ALICE, ALICE.upper()], db_session=session)

    assert roster_plugin.rosters == [[ALICE]]


def test_normalization_keeps_the_first_spelling(session, incident, roster_plugin):
    create_conference(incident=incident, participants=[ALICE.upper(), ALICE], db_session=session)

    assert roster_plugin.rosters == [[ALICE.upper()]]


@pytest.mark.parametrize(
    "given,expected",
    [
        ([], []),
        (None, []),
        ([ALICE], [ALICE]),
        ([ALICE, BOB, CAROL], [ALICE, BOB, CAROL]),
        ([ALICE, ALICE], [ALICE]),
        ([ALICE, BOB, ALICE, CAROL, BOB], [ALICE, BOB, CAROL]),
        ([ALICE, ALICE.upper(), ALICE.title()], [ALICE]),
        ([ALICE, "", BOB], [ALICE, BOB]),
        ([ALICE, None, BOB], [ALICE, BOB]),
        (
            ["straße@example.invalid", "strasse@example.invalid"],
            ["straße@example.invalid", "strasse@example.invalid"],
        ),
        ([CAROL, BOB, ALICE], [CAROL, BOB, ALICE]),
    ],
)
def test_normalize_participants(given, expected):
    assert normalize_participants(given) == expected


# --- the tactical group branch ------------------------------------------------


def opt_out(session, email: str) -> str:
    from dispatch.auth.models import DispatchUserSettings
    from tests.factories import DispatchUserFactory

    user = DispatchUserFactory(email=email)
    session.add(DispatchUserSettings(dispatch_user_id=user.id, auto_add_to_incident_bridges=False))
    session.commit()
    return email


def test_a_tactical_group_is_never_the_roster(session, incident):
    """The responders themselves, not the group's address.

    Until #110 this branch substituted `[tactical_group.email]`, which was
    harmless while Google Calendar -- where a Google Group is a first-class
    attendee -- was the only plugin reading the list. It does not survive the
    roster reaching Graph, whose `upn` is a *user* principal name, and it makes
    the bridge preference unexpressible in every deployment that runs a group
    plugin, which is most of them.
    """
    incident.tactical_group = GroupFactory(email="incident-tactical@example.invalid")
    session.commit()

    assert filter_participants_for_bridge([ALICE, BOB], session) == [ALICE, BOB]


def test_the_preference_decides_even_when_a_tactical_group_exists(session, incident):
    """The case the old compaction silently overrode. A tactical group exists in
    the normal deployment, so if the group address won here the preference would
    decide nothing in practice -- the same defect #110 was filed for, one layer
    up."""
    opted_out = opt_out(session, "opted-out-under-a-group@example.invalid")

    incident.tactical_group = GroupFactory(email="incident-tactical@example.invalid")
    session.commit()

    assert filter_participants_for_bridge([ALICE, opted_out], session) == [ALICE]


def test_the_preference_decides_without_a_tactical_group(session, incident):
    opted_out = opt_out(session, "opted-out-without-a-group@example.invalid")

    assert incident.tactical_group is None
    assert filter_participants_for_bridge([ALICE, opted_out], session) == [ALICE]


def test_nobody_opting_in_still_yields_a_roster_the_flow_accepts(session, incident):
    opted_out = opt_out(session, "everyone-opted-out@example.invalid")

    assert filter_participants_for_bridge([opted_out], session) == []


# --- the wiring, end to end ---------------------------------------------------


def test_incident_create_resources_seeds_the_bridge_with_the_filtered_roster(
    session, incident, roster_plugin, monkeypatch
):
    """The only test that states issue #110's claim end to end.

    Every other test here calls `create_conference` or the filter directly, so
    all of them would survive `incident_create_resources` being reverted to pass
    `tactical_participant_emails` raw -- which is the line that decides whether
    any of this reaches a provider.

    Everything the flow fans out to besides the conference is stubbed. That is
    not the conference plugin being mocked -- it is registered for real and
    records what it was handed; it is the ticket, group, storage, document and
    conversation integrations, none of which this assertion is about.
    """
    from dispatch.incident import flows as incident_flows

    opted_in = "opted-in-e2e@example.com"
    opted_out = opt_out(session, "opted-out-e2e@example.com")

    monkeypatch.setattr(
        incident_flows,
        "get_incident_participants",
        lambda incident, db_session: (
            [(SimpleNamespace(email=opted_in), None), (SimpleNamespace(email=opted_out), None)],
            [],
        ),
    )
    for module, name in [
        (incident_flows.ticket_flows, "create_incident_ticket"),
        (incident_flows.ticket_flows, "update_incident_ticket"),
        (incident_flows.group_flows, "create_group"),
        (incident_flows.storage_flows, "create_storage"),
        (incident_flows.document_flows, "create_document"),
        (incident_flows.document_flows, "update_document"),
        (incident_flows.conversation_flows, "create_incident_conversation"),
        (incident_flows.conversation_flows, "set_conversation_topic"),
        (incident_flows.conversation_flows, "set_conversation_description"),
        (incident_flows.conversation_flows, "add_conversation_bookmark"),
        (incident_flows.conversation_flows, "add_incident_participants_to_conversation"),
        (incident_flows.canvas_flows, "create_participants_canvas"),
    ]:
        monkeypatch.setattr(module, name, lambda *a, **kw: None)
    monkeypatch.setattr(
        incident_flows, "send_incident_created_notifications", lambda *a, **kw: None
    )
    monkeypatch.setattr(incident_flows, "bulk_participant_announcement_message", lambda **kw: None)
    monkeypatch.setattr(
        incident_flows, "send_incident_welcome_participant_messages", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        incident_flows, "incident_add_or_reactivate_participant_flow", lambda *a, **kw: None
    )

    incident_flows.incident_create_resources(incident=incident, db_session=session)

    assert roster_plugin.rosters == [[opted_in]], (
        "the bridge must be created listing exactly the responders who did not opt out"
    )


# --- the interface every conference plugin has to satisfy ---------------------


def test_every_conference_plugin_accepts_the_call_create_conference_makes():
    """`ConferencePlugin.create` stopped being `(self, items, **kwargs)` when the
    roster became part of the documented interface (issue #110). The existing
    parity test compares Zoom against Teams only, and compares parameter *names*
    -- so it would not notice a plugin the flow can no longer call.

    Binds the arguments `create_conference` actually passes, against every
    registered conference plugin including the base class and the test stub.
    """
    import inspect

    from dispatch.plugins.dispatch_google.calendar.plugin import GoogleCalendarConferencePlugin
    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )
    from dispatch.plugins.dispatch_test.conference import TestConferencePlugin
    from dispatch.plugins.dispatch_zoom.plugin import ZoomConferencePlugin

    for cls in (
        ConferencePlugin,
        ZoomConferencePlugin,
        MicrosoftTeamsConferencePlugin,
        GoogleCalendarConferencePlugin,
        TestConferencePlugin,
        RosterRecordingPlugin,
    ):
        signature = inspect.signature(inspect.unwrap(cls.create))
        # As `create_conference` calls it: name positional, the rest by keyword.
        (
            signature.bind(object(), "incident-1", title="Situation Room", participants=[ALICE]),
            f"{cls.__name__}.create cannot be called the way create_conference calls it",
        )


def test_dropping_an_address_less_participant_is_logged(caplog):
    """The roster is the one place a contact with no email would be swallowed --
    `normalize_participants` skips it so one bad row cannot cost the incident its
    whole bridge, which is only defensible if it says so."""
    import logging

    with caplog.at_level(logging.WARNING, logger="dispatch.conference.flows"):
        assert normalize_participants([ALICE, "", None, BOB]) == [ALICE, BOB]

    assert len(caplog.records) == 1
    assert "2" in caplog.records[0].getMessage()


def test_a_complete_roster_logs_nothing(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="dispatch.conference.flows"):
        normalize_participants([ALICE, BOB])

    assert caplog.records == []
