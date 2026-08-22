"""Microsoft Teams conference plugin."""

import logging

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import ConferenceCreatedButUnusable, ConferenceRosterUnreadable
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


def reported_attendees(meeting) -> list[dict] | None:
    """The meeting's attendee roster as Graph reported it, or None if it did not.

    The parameter is untyped and the shape guards are total, so this is safe to
    point at any parsed body. `MSTeamsClient._parse` already rejects a non-object
    before `get_meeting` returns, so the outer `isinstance` never fires on this
    plugin's own path -- it is what lets the live suite call this directly.

    Graph documents `participants` as a property of the onlineMeeting a GET
    returns, and the app-token response example on that reference -- the same
    flow this plugin uses -- reports `"attendees": []` for a meeting with none,
    so the key is present either way. Documented, not measured: no tenant has
    been read against, and `test_teams_live.py` is what would settle it.

    The tri-state is kept regardless, because the alternative is a *destructive*
    guess: the list is only ever written wholesale, so a caller that reads
    "nothing" and writes "one" silently discards whatever Graph is holding. A
    list -- `[]` included -- is Graph answering. Anything else is Graph not
    answering, and is reported as None so the write side refuses instead:

    - `attendees` absent, or an explicit null;
    - `participants` absent, or not an object;
    - a roster holding an entry that is not an object, or one whose `upn` is
      neither a string nor null. Neither can be rewritten faithfully, and a
      non-string `upn` would otherwise reach `matches` and raise
      `AttributeError`, which lands on the incident timeline reading like a
      Dispatch bug rather than a provider one.

    An entry with a null `upn` is *not* in that group: a phone or anonymous
    attendee is a well-formed attendee Graph chose to send, and it round-trips
    unchanged.

    The claim this replaces -- "Graph sends an explicit null for both keys" --
    was not evidence. It was written in `97380231` as a word-for-word twin of a
    sentence asserting the same of Zoom, which has since been measured false
    there. A pattern applied to two providers, and a reading of neither.
    """
    participants = meeting.get("participants") if isinstance(meeting, dict) else None
    if not isinstance(participants, dict):
        return None

    attendees = participants.get("attendees")
    if not isinstance(attendees, list):
        return None
    if not all(
        isinstance(a, dict) and isinstance(a.get("upn"), (str, type(None))) for a in attendees
    ):
        return None

    return list(attendees)


def describe_roster(meeting) -> str:
    """Which shape Graph answered with, in a phrase safe to repeat anywhere.

    `reported_attendees` collapses four distinct non-answers into None, which is
    right for deciding whether to write and useless for saying why. This names
    them, so the refusal that reaches the application log carries the fact an
    operator needs. A count and a category only -- never an address, never an
    identifier.
    """
    if not isinstance(meeting, dict) or not isinstance(meeting.get("participants"), dict):
        return "the response carried no participants object"

    participants = meeting["participants"]
    if "attendees" not in participants:
        return "participants were returned without an attendees key"

    attendees = participants["attendees"]
    if attendees is None:
        return "attendees was an explicit null"
    if not isinstance(attendees, list):
        return f"attendees was a {type(attendees).__name__} rather than a list"

    count = len(attendees)
    plural = "y" if count == 1 else "ies"
    if reported_attendees(meeting) is None:
        return f"attendees held {count} entr{plural} this plugin cannot rewrite faithfully"
    if not count:
        return "attendees was an empty list"
    return f"attendees held {count} entr{plural}"


def current_attendees(meeting: dict) -> list[dict]:
    """The meeting's attendees, or a refusal when Graph did not report them.

    `update_attendees` replaces the list wholesale -- Graph's reference is
    explicit that adjusting it "always requires the full list of attendees in
    the request body" -- so every roster change is a rewrite of the whole list.
    A rewrite built on a read that reported nothing discards whatever Graph is
    holding, which is exactly how a roster seeded at create time (issue #110)
    would vanish on the first responder to join (issue #130).

    Refusing costs one entry on a list that gates nothing, and
    `update_conference_participant` logs it and carries on. Guessing costs the
    founding roster, silently.
    """
    attendees = reported_attendees(meeting)
    if attendees is None:
        raise ConferenceRosterUnreadable(
            f"Microsoft Graph did not report this meeting's attendee list, so its roster "
            f"was left alone ({describe_roster(meeting)}): the API replaces that list "
            f"wholesale, and rebuilding it from a read that reported nothing would drop "
            f"the attendees already on it. The list does not control who can join -- the "
            f"meeting link works for whoever holds it either way."
        )

    return attendees


def as_attendee(participant: str) -> dict:
    """One participant as Graph's attendee list represents them.

    Shared by `create` and `add_participant` so the two never disagree about what
    an attendee looks like -- they write to the same list, and Graph replaces it
    wholesale.

    `attendee` is the only role safe to assign unconditionally: presenter and
    coorganizer are unsupported for identities Entra cannot resolve, and
    responders may be external. `identity` is left to Graph, which is documented
    as optional and could not be populated without resolving the address to an
    Entra object id -- that needs User.Read.All, which this plugin does not ask
    for.
    """
    return {"upn": participant, "role": "attendee"}


def matches(attendee: dict, participant: str) -> bool:
    """Whether an attendee is the given participant. UPNs are case-insensitive."""
    upn = attendee.get("upn")
    return bool(upn) and upn.casefold() == participant.casefold()


def find_attendee(attendees: list[dict], participant: str) -> dict | None:
    return next((a for a in attendees if matches(a, participant)), None)


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

        `participants` becomes the meeting's initial attendee roster (issue
        #110). Invitation metadata as far as Dispatch is concerned: responders
        join through the link Dispatch publishes, and nothing here gates that
        link. **Inferred, and the one caveat worth knowing:** a tenant whose lobby
        policy admits only invited people (`lobbyBypassSettings.scope = invited`,
        which this plugin never sets) may treat the attendee list as its
        bypass list, so an absent responder could wait in the lobby rather than
        be kept out. Graph does not document whether a standalone onlineMeeting's
        attendee counts as "invited" there.

        `description` is accepted for interface parity and unused: an
        onlineMeeting has no agenda field.

        A roster Graph rejects fails the create, and that is the intended
        behaviour: the meeting does not exist yet, so nothing is stranded and the
        error names the reason. Nothing is truncated to fit -- Graph's documented
        limits are about contact lists larger than 150 and 1000 members, and a
        silently shortened roster would be a worse answer than a failed create.
        """
        meeting = self._client().create_meeting(
            subject=title if title else f"Situation Room for {name}",
            duration_minutes=self.configuration.default_duration_minutes,
            record_automatically=self.configuration.allow_auto_recording,
            require_passcode=self.configuration.require_passcode,
            attendees=[as_attendee(p) for p in participants or []],
        )

        # Graph has committed the meeting by the time it answers 2xx, so every
        # rejection below strands a live bridge. They all raise
        # `ConferenceCreatedButUnusable`, which is what tells `create_conference`
        # to delete it -- carrying the id when the response yielded one, and
        # None when it did not, which still gets the possible leak logged rather
        # than filed under "the provider was down" (issue #114).
        #
        # conference/flows.py subscripts these without a default, so an
        # incomplete meeting has to fail here rather than downstream. The body
        # itself is deliberately not quoted -- it carries the passcode and the
        # dial-in conference id, and this message reaches the incident timeline.
        #
        # Collected rather than raised on the first miss, so a meeting missing
        # both still reports the absent id -- the fact that explains why nothing
        # could be cleaned up.
        missing = [field for field in ("joinWebUrl", "id") if not meeting.get(field)]
        if missing:
            raise ConferenceCreatedButUnusable(
                f"Microsoft Graph created a meeting without {', '.join(missing)}.",
                # Never a fallback to joinWebUrl or subject: those are not
                # unique and could name another incident's meeting.
                resource_id=meeting.get("id") or None,
            )

        try:
            challenge = build_challenge(meeting)
        except Exception as e:
            # `build_challenge` runs after the id guard, so a malformed
            # `joinMeetingIdSettings` would otherwise throw away a meeting id we
            # are holding.
            raise ConferenceCreatedButUnusable(
                "Microsoft Graph created a meeting whose passcode settings could not be read.",
                resource_id=meeting["id"],
            ) from e

        return {
            "weblink": meeting["joinWebUrl"],
            "id": meeting["id"],
            "challenge": challenge,
        }

    def delete(self, event_id: str):
        """Deletes an existing event."""
        self._client().delete_meeting(event_id)

    def add_participant(self, event_id: str, participant: str):
        """Add a participant to the meeting's attendee roster.

        Roster metadata only: the join link works without this, and this does not
        grant access. Graph requires the complete attendee list on every update,
        so this reads the meeting first and resends the existing attendees
        untouched -- including the identity Graph resolved for them, which a
        upn-only round trip would discard. A read that does not report the
        roster raises `ConferenceRosterUnreadable` and writes nothing rather
        than replacing it with this one participant (issue #130).
        """
        client = self._client()
        attendees = current_attendees(client.get_meeting(event_id))

        if find_attendee(attendees, participant) is not None:
            return

        # Built by `as_attendee`, which carries the reasoning about role and
        # identity. Unverified against a live tenant, and there are reports of
        # Graph answering 200 to an attendee update made with application
        # permissions without applying it -- so the live suite asserts by reading
        # the meeting back rather than trusting the status.
        client.update_attendees(event_id, attendees + [as_attendee(participant)])

    def remove_participant(self, event_id: str, participant: str):
        """Remove a participant from the meeting's attendee roster.

        This does not evict anyone or revoke the join link; it only updates the
        roster. Absent participants are left alone rather than treated as errors,
        so a retried removal is a no-op, and a read that does not report the
        roster raises `ConferenceRosterUnreadable` rather than rewriting the
        list from nothing (issue #130).
        """
        client = self._client()
        attendees = current_attendees(client.get_meeting(event_id))

        remaining = [a for a in attendees if not matches(a, participant)]
        if len(remaining) == len(attendees):
            return

        client.update_attendees(event_id, remaining)
