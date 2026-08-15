"""A fake Google Calendar API client for the conference plugin tests.

The fake sits at the ``googleapiclient`` boundary -- ``client.events().insert(
**kwargs).execute()`` -- so ``create_event`` and ``make_call`` both run their
real code against it. Nothing patches ``create_event`` or ``make_call``
themselves; that is the point, since the defect in issue #122 lives precisely
in how those two compose.

It models the two Google semantics the fix depends on, both verified against
the published API reference rather than assumed:

- ``events.insert`` is idempotent **only** on the client-supplied event ``id``.
  Re-inserting an id that already exists on the calendar answers HTTP 409
  ("The requested identifier already exists"), it does not create a second
  event. An insert with no ``id`` always creates a new event, which is what
  makes an unguarded retry duplicate.
- ``conferenceData.createRequest.requestId`` deduplicates the *conference
  creation request*, not the event. It is deliberately NOT modelled as
  preventing duplicate events, because it does not.
"""

import json
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError


def http_error(status: int, message: str = "boom", reason: str | None = None) -> HttpError:
    """An ``HttpError`` shaped like Google's real JSON error body.

    ``reason`` is the machine-readable code (``rateLimitExceeded``,
    ``forbidden``, ...). Google returns 403 for both rate limiting and genuine
    permission failures, so the reason -- not the status alone -- is what
    decides retryability.
    """
    body = {"error": {"code": status, "message": message}}
    if reason is not None:
        body["error"]["errors"] = [{"reason": reason, "message": message}]

    response = MagicMock()
    response.status = status
    response.reason = message
    return HttpError(response, json.dumps(body).encode(), uri="https://example.test")


class Call:
    """One recorded API invocation."""

    def __init__(self, method: str, kwargs: dict):
        self.method = method
        self.kwargs = kwargs

    @property
    def body(self) -> dict:
        return self.kwargs.get("body") or {}

    @property
    def event_id(self):
        return self.body.get("id")

    @property
    def request_id(self):
        create = (self.body.get("conferenceData") or {}).get("createRequest") or {}
        return create.get("requestId")


class FakeCalendar:
    """Routes the calendar endpoints this plugin touches and records every call.

    ``failures`` is a list of exceptions (or ``None`` for "succeed") consumed
    one per ``insert`` attempt, which is how a test scripts a transient
    failure. ``fail_after_create`` makes the failure happen *after* the event
    is stored -- the dangerous case from issue #122, where Google accepted the
    write and the client never learned it.
    """

    def __init__(self, failures=None, fail_after_create: bool = False, delete_failures=None):
        self.calls: list[Call] = []
        self.events_store: dict[str, dict] = {}
        self.failures = list(failures or [])
        self.fail_after_create = fail_after_create
        # Consumed one per ``delete`` attempt, the same way ``failures`` is
        # consumed per ``insert``. Only needed for statuses the store cannot
        # produce on its own -- 410, say, which Calendar answers for an event it
        # remembers deleting.
        self.delete_failures = list(delete_failures or [])
        self._auto_id = 0

    # --- the recorded surface ------------------------------------------

    def insert_calls(self) -> list[Call]:
        return [c for c in self.calls if c.method == "insert"]

    def request_ids(self) -> list[str]:
        return [c.request_id for c in self.insert_calls()]

    def event_ids(self) -> list[str]:
        return [c.event_id for c in self.insert_calls()]

    # --- google semantics ----------------------------------------------

    def _store(self, body: dict) -> dict:
        event_id = body.get("id")
        if event_id is None:
            self._auto_id += 1
            event_id = f"server-generated-{self._auto_id}"

        stored = {
            **body,
            "id": event_id,
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": f"https://meet.google.com/{event_id}"}
                ]
            },
        }
        self.events_store[event_id] = stored
        return stored

    def _insert(self, **kwargs) -> dict:
        body = kwargs.get("body") or {}
        event_id = body.get("id")

        # Idempotency: an id we already hold is a duplicate, not a new event.
        if event_id is not None and event_id in self.events_store:
            raise http_error(409, "The requested identifier already exists.", "duplicate")

        if self.fail_after_create and self.failures:
            # Google committed the write, then the response was lost.
            self._store(body)
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure

        elif self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
            return self._store(body)

        return self._store(body)

    def _get(self, **kwargs) -> dict:
        event_id = kwargs.get("eventId")
        if event_id not in self.events_store:
            raise http_error(404, "Not Found", "notFound")
        return self.events_store[event_id]

    def _update(self, **kwargs) -> dict:
        event_id = kwargs.get("eventId")
        self.events_store[event_id] = kwargs.get("body") or {}
        return self.events_store[event_id]

    def _delete(self, **kwargs) -> None:
        if self.delete_failures:
            failure = self.delete_failures.pop(0)
            if failure is not None:
                raise failure

        # Calendar 404s a delete naming an event it does not hold, rather than
        # answering "already done". That refusal is the whole of issue #120.
        event_id = kwargs.get("eventId")
        if event_id not in self.events_store:
            raise http_error(404, "Not Found", "notFound")

        del self.events_store[event_id]
        return None

    # --- the googleapiclient shape --------------------------------------

    def events(self):
        return _Events(self)


class _Request:
    def __init__(self, fn, method, kwargs, recorder):
        self._fn = fn
        self._method = method
        self._kwargs = kwargs
        self._recorder = recorder

    def execute(self):
        self._recorder.calls.append(Call(self._method, self._kwargs))
        return self._fn(**self._kwargs)


class _Events:
    def __init__(self, calendar: FakeCalendar):
        self._calendar = calendar

    def insert(self, **kwargs):
        return _Request(self._calendar._insert, "insert", kwargs, self._calendar)

    def get(self, **kwargs):
        return _Request(self._calendar._get, "get", kwargs, self._calendar)

    def update(self, **kwargs):
        return _Request(self._calendar._update, "update", kwargs, self._calendar)

    def delete(self, **kwargs):
        return _Request(self._calendar._delete, "delete", kwargs, self._calendar)
