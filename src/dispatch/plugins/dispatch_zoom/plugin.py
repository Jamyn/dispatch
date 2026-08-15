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
from dispatch.exceptions import (
    ConferenceCreatedButUnusable,
    ConferenceRosterUnreadable,
    DispatchPluginException,
)
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


def reported_invitees(meeting) -> list[dict] | None:
    """The meeting's invitee roster as Zoom reported it, or None if it did not.

    Takes whatever `get_meeting` parsed out of the response, which is why the
    parameter is untyped: a JSON body is not obliged to be an object.

    **Verified against a real Zoom account 2026-08-15 (issue #129):** a
    `GET /meetings/{meetingId}` reports `settings.meeting_invitees` in full,
    and a meeting with no invitees reports it as `[]` -- the key is present
    either way. So Zoom answers the question, the read-modify-write below is
    sound, and the staff claim that invitees cannot be queried back is wrong
    for this endpoint on this account tier.

    The tri-state is kept anyway, because it costs nothing and the alternative
    is a *destructive* guess: the list is only ever written wholesale, so a
    caller that reads "nothing" and writes "one" silently discards whatever was
    there. A list -- `[]` included -- is Zoom answering. Anything else is Zoom
    not answering, and is reported as None so the write side refuses instead:

    - the key absent, or an explicit null;
    - `settings` absent, or not an object;
    - a roster holding an entry that is not an object, or one whose `email` is
      neither a string nor null. Neither can be rewritten faithfully, so neither
      is rewritten at all -- and a non-string `email` would otherwise reach
      `invitee_matches` and raise `AttributeError`, which lands on the incident
      timeline reading like a Dispatch bug rather than a provider one.

    An entry with a null `email` is *not* in that group: it is a well-formed
    invitee Zoom chose to send, and it round-trips unchanged.

    The claim this replaces -- "Zoom sends an explicit null rather than omitting
    the key" -- was **not** evidence. It was written in `97380231` in the same
    breath as a word-for-word twin asserting it of Microsoft Graph, when no Zoom
    account existed to have observed it against. It is now also **measured
    false**: Zoom sends `[]`, not null. A pattern applied to two providers, and
    a reading of neither.

    Entries are otherwise passed through as they arrive. Zoom's read schema
    carries an `internal_user` its write schema does not, and that is resent
    verbatim -- **verified safe 2026-08-15**: a roster read back as
    `{"email": ..., "internal_user": false}` and PATCHed straight back is
    accepted, and Zoom re-derives the field on the next read. Do not "clean" it
    to `{"email": ...}`; that would be a guess in the other direction, and this
    one has been run.
    """
    settings = meeting.get("settings") if isinstance(meeting, dict) else None
    if not isinstance(settings, dict):
        return None

    invitees = settings.get("meeting_invitees")
    if not isinstance(invitees, list):
        return None
    if not all(
        isinstance(invitee, dict) and isinstance(invitee.get("email"), (str, type(None)))
        for invitee in invitees
    ):
        return None

    return list(invitees)


def describe_roster(meeting: dict) -> str:
    """Which shape Zoom answered with, in a phrase safe to repeat anywhere.

    `reported_invitees` collapses four distinct non-answers into None, which is
    right for deciding whether to write and useless for saying why. This names
    them, so the refusal that reaches the incident timeline -- and the live
    suite's failure message -- carry the one fact an operator needs to record on
    issue #129 rather than a single string covering four different providers.

    Kept beside `reported_invitees` so the two cannot drift; the live suite
    imports this rather than reimplementing it. Names no address and no
    identifier: a count and a category only.
    """
    if not isinstance(meeting, dict) or not isinstance(meeting.get("settings"), dict):
        return "the response carried no settings object"

    settings = meeting["settings"]
    if "meeting_invitees" not in settings:
        return "settings were returned without a meeting_invitees key"

    invitees = settings["meeting_invitees"]
    if invitees is None:
        return "meeting_invitees was an explicit null"
    if not isinstance(invitees, list):
        return f"meeting_invitees was a {type(invitees).__name__} rather than a list"
    if reported_invitees(meeting) is None:
        return f"meeting_invitees held {len(invitees)} entr{'y' if len(invitees) == 1 else 'ies'} this plugin cannot rewrite faithfully"
    if not invitees:
        return "meeting_invitees was an empty list"
    return f"meeting_invitees held {len(invitees)} entr{'y' if len(invitees) == 1 else 'ies'}"


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


def current_invitees(client, event_id: str) -> list[dict]:
    """The meeting's roster, or a refusal when Zoom did not report one.

    `update_invitees` replaces the list wholesale and Zoom publishes no append
    or remove primitive for it, so every roster change is a rewrite of the
    whole list. A rewrite built on a read that reported nothing discards
    whatever Zoom is holding -- which is exactly how a roster seeded at create
    time (issue #127) would vanish on the first responder to join.

    Against a real account this never fires: Zoom reports the roster, and
    reports `[]` for a meeting that has none (verified 2026-08-15), so there is
    no ordinary read it declines. It is the guard for the case where that stops
    being true. Refusing then costs one entry on a list Zoom's own staff
    describe as consumed only by their calendar integrations, and
    `update_conference_participant` logs it and carries on. Guessing would cost
    the founding roster, silently.

    The message names the shape Zoom actually answered with, because there are
    four ways to not answer and they are different facts -- in a deployment
    where this fires, it is the evidence that settles issue #129. It also says
    outright that the list does not gate joining, since it is read by whoever is
    running the incident and "could not be added" invites the opposite reading.

    `[]` is trusted, and that is measured rather than assumed: a meeting created
    with no invitees really does read back as `[]`, and one created with two
    really does read back as those two. `log_roster_echo` is the canary if an
    account ever behaves otherwise.
    """
    meeting = get_meeting(client, event_id)
    invitees = reported_invitees(meeting)
    if invitees is None:
        raise ConferenceRosterUnreadable(
            f"Zoom did not report this meeting's invitee list, so its roster was left "
            f"alone ({describe_roster(meeting)}): the API replaces that list wholesale, "
            f"and rebuilding it from a read that reported nothing would drop the invitees "
            f"already on it. The list does not control who can join -- the meeting link "
            f"works for whoever holds it either way."
        )

    return invitees


def log_roster_echo(conference_json: dict, requested: list[str]) -> None:
    """Record whether Zoom echoed back the roster it was just sent (issue #129).

    Zoom documents `settings.meeting_invitees` on the create's own 201, so this
    reads a response already in hand and costs no extra call. It is the only
    moment Dispatch holds both what it sent and what came back, which makes it
    the one place that can catch the outcome the read side cannot defend
    against: an account that stores the roster and reports it short. `[]` is
    indistinguishable at update time from a meeting that has no invitees, so
    `current_invitees` trusts it -- correctly, on the account this was verified
    against, where a seeded roster reads back in full. This stays as the canary
    for an account where that is not true.

    Deliberately says nothing about what a *read* will do. The 201 and the 200
    are different schemas -- the GET's item carries an `internal_user` the
    create's does not -- so an absent field here is not evidence about
    `GET /meetings/{meetingId}`. Claiming otherwise would send an operator to
    close issue #129 on the wrong observation.

    Counts only, never addresses -- this reaches the application log, and the
    roster is the incident's responder list.
    """
    echoed = reported_invitees(conference_json)

    if echoed is None:
        log.warning(
            "Zoom accepted %s conference invitee(s) and its create response carried no "
            "usable invitee list (%s). Whether a read reports them is a separate "
            "question -- see issue #129.",
            len(requested),
            describe_roster(conference_json),
        )
        return

    # Compared against the distinct addresses sent, not the raw count: the
    # plugin forwards the roster it is given and `create_conference` is what
    # deduplicates, so a provider that collapses duplicates is not dropping
    # anyone and must not be reported as though it were.
    expected = len({participant.casefold() for participant in requested if participant})

    if len(echoed) < expected:
        log.warning(
            "Zoom accepted %s distinct conference invitee(s) and reported %s back on the "
            "create response. If the rest are in fact stored, later roster updates will "
            "replace them rather than extend them (issue #129).",
            expected,
            len(echoed),
        )
    else:
        log.info(
            "Zoom reported %s of %s distinct conference invitee(s) back on the create response.",
            len(echoed),
            expected,
        )


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

    **The roster is readable, verified 2026-08-15 (issue #129).** A real
    account returns `settings.meeting_invitees` in full on the 200 of
    `GET /meetings/{meetingId}`, with the `internal_user` the write schemas
    cannot send. So it is stored, not merely accepted, and `add_participant`
    maintains it correctly. The developer-forum claim that invitees cannot be
    queried back does not hold for this endpoint.

    Still true, and separate: staff say the field is consumed only by their
    calendar integrations, so an invitee may never *surface* in the Zoom client.
    This decides whether the roster is stored, not whether anyone sees it.

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
        requested = list(participants or [])
        client = self._zoom_client()

        conference_response = create_meeting(
            client,
            self.configuration.api_user_id,
            name,
            description=description,
            title=title,
            participants=requested,
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

        # After the checks above, so a create that failed validation is reported
        # as that rather than as a roster observation. Only when a roster was
        # actually sent: there is nothing to echo otherwise.
        #
        # Nothing between Zoom's 2xx and the return below may raise anything but
        # `ConferenceCreatedButUnusable` -- that exception is what carries the id
        # to `create_conference`'s compensating delete, and anything else leaves
        # a live meeting with no database row, unreachable forever (issue #114).
        # This call is guarded by being total rather than by a try, and the
        # obvious next step for issue #129 -- a confirming GET here -- would not
        # be.
        if requested:
            log_roster_echo(conference_json, requested)

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

        Verified end to end against a real account 2026-08-15: `create([A, B])`
        then `add(C)` reads back as `[A, B, C]` (issue #129).

        That works because Zoom reports the roster. `current_invitees` refuses
        when it does not, which against a real account never happens -- a
        safety net, not a workaround. Keep it: adding one person must never be
        what removes the rest, and a wholesale replace rebuilt from a read that
        answered nothing is exactly how that would happen.
        """
        client = self._zoom_client()
        invitees = current_invitees(client, event_id)

        if any(invitee_matches(i, participant) for i in invitees):
            return

        update_invitees(client, event_id, invitees + [as_invitee(participant)])

    def remove_participant(self, event_id: str, participant: str):
        """Remove a participant from the meeting's invitee roster.

        This evicts nobody and revokes no link; it only updates the roster. An
        absent participant is left alone rather than treated as an error, so a
        retried removal is a no-op.

        Verified against a real account 2026-08-15: `create([A, B])`, `add(C)`,
        `remove(A)` reads back as `[B, C]` (issue #129).

        Refuses on an unreported roster for the same reason as `add_participant`,
        though the danger is not the same: rewriting the list here could only
        ever shorten it. It refuses anyway so the two halves of the lifecycle
        answer alike, and so a removal Zoom cannot be told about is reported
        rather than mistaken for one it already knew.

        `invitee_matches` folds case, which is the *unsafe* direction here in a
        way it is not for adding: an over-aggressive fold costs the add an
        unnecessary skip, and costs this the wrong mailbox. `strasse@x` and
        `straße@x` are different addresses that `casefold` equates, so removing
        one strips the other. `normalize_participants` keeps them apart with
        `lower()` at create time and is not in this path to do so here.
        Deliberately left alone: roster metadata, and changing the comparison on
        one side only would make add and remove disagree about who is present.
        """
        client = self._zoom_client()
        invitees = current_invitees(client, event_id)

        remaining = [i for i in invitees if not invitee_matches(i, participant)]
        if len(remaining) == len(invitees):
            return

        update_invitees(client, event_id, remaining)
