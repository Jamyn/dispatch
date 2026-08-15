"""
.. module: dispatch.plugins.dispatch_google_calendar.plugin
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Kevin Glisson <kglisson@netflix.com>
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from http.client import HTTPException
from typing import Any

from googleapiclient.errors import HttpError
from pytz import timezone
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from dispatch.exceptions import ConferenceAlreadyGone, ConferenceCreatedButUnusable
from dispatch.decorators import apply, counter, timer
from dispatch.plugins.bases import ConferencePlugin
from dispatch.plugins.dispatch_google import calendar as google_calendar_plugin
from dispatch.plugins.dispatch_google.common import get_service
from dispatch.plugins.dispatch_google.config import GoogleConfiguration


log = logging.getLogger(__name__)

# Statuses worth another attempt. Everything absent from this set is treated as
# permanent: retrying only delays the report and, until issue #122, replaced the
# reason Google gave with a bare `TryAgain`.
#
# Two deliberate divergences from Google's "Handle API errors" guide, both
# checked against it rather than assumed:
#   - 429 and 500 are the ones it names. 502/504 are not mentioned at all; they
#     are included because they are gateway failures that say nothing about
#     whether the request was processed, which is the same case as a dropped
#     connection.
#   - It also lists 404 as "use exponential backoff". That is excluded here: on
#     the calls this module makes, a 404 means the caller named an event that is
#     not there, and three retries only postpone saying so.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# 403 means two different things. Google returns it for genuine permission
# failures *and* for rate limiting, and only the latter can succeed on a retry,
# so 403 is classified by reason rather than by status.
RETRYABLE_403_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})

# Google answers 409 to an insert whose client-supplied event id already exists.
# `create_event` relies on this to recognise its own earlier attempt.
DUPLICATE_STATUS = 409

# Calendar 404s a delete naming an event it does not hold, and 410s one it
# remembers deleting. Both mean the same thing to teardown: it is not there
# (issue #120). Read only by `delete_event` -- see the note there about why this
# is not a `make_call` concern.
GONE_STATUSES = frozenset({404, 410})

# Failures below the API: the request may or may not have been processed, and
# the exception cannot say which. httplib2 surfaces more than `ConnectionError`
# and `TimeoutError` -- `ssl.SSLError` and `socket.error` are `OSError`s, while
# `BadStatusLine` and `IncompleteRead` are `HTTPException`s -- so both bases are
# named rather than the two most obvious subclasses.
TRANSPORT_ERRORS = (OSError, HTTPException)


def _error_reasons(error: HttpError) -> set[str]:
    """The machine-readable reasons on a Google error body.

    `HttpError.reason` is the human message, not the reason code, so the codes
    are read out of `error_details` -- which googleapiclient fills from
    `error.errors` -- rather than parsed out of the message text.

    googleapiclient sets `error_details` from the *first* of
    `detail`/`details`/`errors`/`message` present in the body, so a body
    carrying only `message` yields a plain string rather than a list. Calendar
    always nests `errors`, but falling back to the raw content keeps a
    rate-limited 403 from being misread as permanent if it ever does not.
    """
    details = getattr(error, "error_details", None) or []
    if isinstance(details, dict):
        details = [details]

    if isinstance(details, list):
        reasons = {d.get("reason") for d in details if isinstance(d, dict) and d.get("reason")}
        if reasons:
            return reasons

    # `error_details` was a string, or carried no reason codes.
    try:
        body = json.loads(error.content.decode("utf-8"))
        errors = body["error"]["errors"]
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeDecodeError):
        return set()

    if not isinstance(errors, list):
        return set()

    return {e.get("reason") for e in errors if isinstance(e, dict) and e.get("reason")}


def is_retryable(exception: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    Transport failures are retryable because they say nothing about whether the
    server acted -- which is exactly why the operation underneath has to be
    idempotent for the retry to be safe. See `create_event`.
    """
    if isinstance(exception, TRANSPORT_ERRORS):
        return True

    if not isinstance(exception, HttpError):
        return False

    status = exception.resp.status
    if status in RETRYABLE_STATUSES:
        return True
    if status == 403:
        return bool(_error_reasons(exception) & RETRYABLE_403_REASONS)

    return False


@retry(
    stop=stop_after_attempt(3),
    retry=retry_if_exception(is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    # Surface the failure Google actually reported rather than a `RetryError`
    # wrapping a `TryAgain` that carries none of it.
    reraise=True,
)
def make_call(client: Any, func: Any, delay: int = None, propagate_errors: bool = False, **kwargs):
    """Make an google client api call.

    Retries only what `is_retryable` accepts. Callers whose operation is not
    idempotent must make it so before relying on this -- a retried create is
    how one logical operation became three calendar events (issue #122).
    """
    data = getattr(client, func)(**kwargs).execute()

    if delay:
        time.sleep(delay)

    return data


def get_event(client: Any, event_id: str):
    """Fetches a calendar event."""
    return make_call(client.events(), "get", calendarId="primary", eventId=event_id)


def remove_participant(client: Any, event_id: int, participant: str):
    """Remove participant from calendar event."""
    event = get_event(client, event_id)

    attendees = []
    for a in event["attendees"]:
        if a["email"] != participant:
            attendees.append(a)

    event["attendees"] = attendees
    return make_call(client.events(), "update", calendarId="primary", eventId=event_id, body=event)


def add_participant(client: Any, event_id: int, participant: str):
    event = get_event(client, event_id)
    event["attendees"].append({"email": participant})
    return make_call(client.events(), "update", calendarId="primary", eventId=event_id, body=event)


def delete_event(client, event_id: int):
    """Delete a calendar event.

    A not-found answer is reported as `ConferenceAlreadyGone` rather than a
    failure: the event is not there, which is what the delete wanted (issue
    #120). Converted here rather than in `make_call`, which every other call
    shares: `create_event` catches `HttpError` itself to recover its own 409
    (issue #122), and a 404 from a read or a roster update deleted nothing.
    """
    try:
        return make_call(client.events(), "delete", calendarId="primary", eventId=event_id)
    except HttpError as e:
        if e.resp.status in GONE_STATUSES:
            raise ConferenceAlreadyGone(
                f"Google Calendar found no event {event_id} to delete (HTTP {e.resp.status})."
            ) from e
        raise


def create_event(
    client,
    name: str,
    description: str = None,
    title: str = None,
    participants: list[str] = None,
    start_time: str = None,
    duration: int = 60000,  # duration in mins ~6 weeks
):
    if participants:
        participants = [{"email": x} for x in participants]
    else:
        participants = []

    # The event id is what makes this insert idempotent, and it is the only
    # thing that does. `requestId` below deduplicates the *conference creation
    # request*, not the event -- the API reference calls it "the client-
    # generated unique ID for this request" -- so on its own it would let a
    # retried insert create a second event, on every attendee's calendar, with
    # a second Meet bridge (issue #122; the issue also has each duplicate
    # emailing the participants, which overstates it -- `sendUpdates` is unset
    # and defaults to notifying no one, though Google does warn that "some
    # emails might still be sent"). Google documents the client-supplied event
    # id as the mechanism that "prevents duplicate event creation if the
    # operation fails at some point after it is successfully executed in the
    # Calendar backend"; a repeat insert answers 409 instead, recovered below.
    #
    # Generated once here, so every attempt tenacity makes inside `make_call`
    # sends the same id -- and two independent creates never share one.
    # `uuid4().hex` is 32 characters of 0-9a-f, inside the documented base32hex
    # alphabet (a-v and digits) and the 5-1024 length bound.
    event_id = uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    body = {
        "id": event_id,
        "description": description if description else f"Situation Room for {name}. Please join.",
        "summary": title if title else f"Situation Room for {name}",
        "attendees": participants,
        "conferenceData": {
            "createRequest": {
                "requestId": request_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "guestsCanModify": True,
    }

    if start_time:
        raw_dt = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S")
        start = timezone("America/Los_Angeles").localize(raw_dt).astimezone(timezone("Etc/UTC"))
    else:
        start = datetime.utcnow()

    end = start + timedelta(minutes=duration)
    body.update(
        {
            "start": {"date": start.isoformat().split("T")[0], "timeZone": "Etc/UTC"},
            "end": {"date": end.isoformat().split("T")[0], "timeZone": "Etc/UTC"},
        }
    )

    # TODO sometimes google is slow with the meeting invite, we should poll/wait
    try:
        return make_call(
            client.events(), "insert", calendarId="primary", body=body, conferenceDataVersion=1
        )
    except (HttpError, *TRANSPORT_ERRORS) as e:
        if isinstance(e, HttpError) and e.resp.status == DUPLICATE_STATUS:
            # The id is a uuid4 minted a few lines above and sent by nothing
            # else, so a duplicate means an earlier attempt of *this* create
            # reached Google after all and only its response was lost. Reading
            # the event back is what keeps that from becoming a second one.
            log.warning(
                "Google reported the calendar event as already created; "
                "recovering the event from an earlier attempt of this create."
            )
            try:
                return get_event(client, event_id)
            except Exception as read_error:
                # Deliberately every exception, not just `HttpError`: this read
                # retries too, and exhausting it raises whatever the transport
                # gave. Google has told us the event exists, so letting anything
                # escape here reaches `create_conference` as "the provider gave
                # us no resource" -- nothing compensates, and the event, with
                # its Meet bridge, is stranded with no Dispatch record at all.
                # The id is the one thing we do know, so it goes out on the
                # exception that exists to carry it (issue #114).
                raise ConferenceCreatedButUnusable(
                    "Google created the calendar event but it could not be read back.",
                    resource_id=event_id,
                ) from read_error

        if not is_retryable(e):
            # A rejection -- bad request, bad credentials, no permission. The
            # insert never took effect, so there is nothing to look for.
            raise

        # Retries were exhausted on a failure that says nothing about whether
        # Google acted, and no 409 ever surfaced to reveal it -- which happens
        # when every attempt dies at a gateway in front of the backend. One
        # probe settles it; without it a committed event would be reported as
        # "nothing was created" and orphaned.
        try:
            recovered = get_event(client, event_id)
        except Exception:
            raise e from None

        log.warning(
            "The calendar insert failed but Google is holding the event; "
            "recovering it rather than reporting the create as failed."
        )
        return recovered


@apply(timer, exclude=["__init__"])
@apply(counter, exclude=["__init__"])
class GoogleCalendarConferencePlugin(ConferencePlugin):
    title = "Google Calendar Plugin - Conference Management"
    slug = "google-calendar-conference"
    description = "Uses Google calendar to manage conference rooms/meets."
    version = google_calendar_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"

    def __init__(self):
        self.scopes = ["https://www.googleapis.com/auth/calendar"]
        self.configuration_schema = GoogleConfiguration

    def create(
        self, name: str, description: str = None, title: str = None, participants: list[str] = None
    ):
        """Create a new event."""
        client = get_service(self.configuration, "calendar", "v3", self.scopes)
        conference = create_event(
            client,
            name,
            description=description,
            participants=participants,
            title=title,
            duration=self.configuration.default_duration_minutes,
        )

        # Google has committed the event by the time `create_event` returns, so
        # every rejection below strands a live Meet bridge. They raise
        # `ConferenceCreatedButUnusable`, which is what tells `create_conference`
        # to delete it, carrying the event id when the response yielded one
        # (issue #114). This is not hypothetical here: Google attaches the
        # conference asynchronously -- see the TODO in `create_event` -- and
        # reports `conferenceData.createRequest.status.statusCode == "pending"`
        # meanwhile, with no `entryPoints` at all.
        #
        # All three plugins seed `participants` on create since issue #110, but
        # only this one *invites*: a calendar event puts the link in responders'
        # mailboxes, where Zoom's invitee list and Teams' attendee list notify
        # nobody. So deleting the event here withdraws real invitations, which is
        # still better than leaving a bridge the incident does not know about and
        # a retry will duplicate.
        event_id = conference.get("id") if isinstance(conference, dict) else None

        try:
            entry_points = conference["conferenceData"]["entryPoints"]
        except (KeyError, TypeError) as e:
            raise ConferenceCreatedButUnusable(
                "Google created the event but has not attached a conference to it.",
                resource_id=event_id,
            ) from e

        meet_url = ""
        for entry_point in entry_points:
            if entry_point.get("entryPointType") == "video":
                meet_url = entry_point.get("uri")

        if not meet_url or not event_id:
            # conference/flows.py subscripts both without a default. The event
            # body is deliberately not quoted -- it carries the attendee list,
            # and this message reaches the incident timeline.
            raise ConferenceCreatedButUnusable(
                "Google created the event without a usable video entry point.",
                # Never a fallback to the summary or the htmlLink: neither is
                # unique and either could name another incident's event.
                resource_id=event_id,
            )

        return {"weblink": meet_url, "id": event_id, "challenge": ""}

    def delete(self, event_id: str):
        """Deletes an existing event."""
        client = get_service(self.configuration, "calendar", "v3", self.scopes)
        return delete_event(client, event_id)

    def add_participant(self, event_id: str, participant: str):
        """Adds a new participant to event."""
        client = get_service(self.configuration, "calendar", "v3", self.scopes)
        return add_participant(client, event_id, participant)

    def remove_participant(self, event_id: str, participant: str):
        """Removes a participant from event."""
        client = get_service(self.configuration, "calendar", "v3", self.scopes)
        return remove_participant(client, event_id, participant)
