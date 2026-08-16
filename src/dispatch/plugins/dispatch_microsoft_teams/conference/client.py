"""Microsoft Graph client for the Teams conference plugin."""

import logging
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import msal
import requests

from dispatch.exceptions import (
    ConferenceAlreadyGone,
    ConferenceCreatedButUnusable,
    DispatchPluginException,
)

log = logging.getLogger(__name__)

GRAPH_API_BASE_URI = "https://graph.microsoft.com/v1.0"


def _quote_path_component(value: str) -> str:
    """Percent-encode one dynamic URL path segment.

    ``safe=""`` is deliberate: a bare ``quote()`` leaves ``/`` unescaped, which
    is exactly the character that lets a value walk out of its own path
    segment (see issue #123). Only ever call this on a single path component,
    never on a full URL or a query string.

    A component that is *exactly* ``.`` or ``..`` is refused rather than
    encoded: ``quote()`` can never escape ``.`` (Python's always-safe set --
    letters, digits, ``_.-~`` -- is only ever added to by ``safe``, never
    subtracted from), and hand-escaping it (``.`` -> ``%2E``) doesn't help
    either -- ``requests.utils.requote_uri``, which every ``PreparedRequest``
    runs through, decodes any percent-triplet of an unreserved character back
    to its literal form before the request line is built, precisely because
    ``.`` is unreserved. So a value of ``".."`` always reaches the wire as a
    literal, un-encoded dot-segment no matter how it was escaped going in.
    Neither Graph nor Microsoft's ``requests`` documents whether the server
    also normalises it away, so this codebase cannot prove it is safe to
    send -- it is refused instead.
    """
    if value in (".", ".."):
        raise DispatchPluginException(
            f"Refusing to build a Microsoft Graph request with a path "
            f"component of {value!r}: it cannot be percent-encoded in a way "
            f"that survives requests' own URL normalization."
        )
    return quote(value, safe="")


_RETRY_AFTER_SECONDS_RE = re.compile(r"^\d+$")


def _looks_like_retry_after(value: str) -> bool:
    """Whether ``value`` has one of the two shapes RFC 9110 defines for it.

    A rogue responder controls every header on a response it forges, not just
    the body, so this is a security check, not a parsing convenience -- a
    value outside both shapes is refused rather than repeated (issue #156).
    """
    if _RETRY_AFTER_SECONDS_RE.match(value):
        return True
    try:
        parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return False
    return True


_REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F-]{1,64}$")


def _looks_like_request_id(value: str) -> bool:
    """Whether ``value`` has the GUID shape Graph documents for ``request-id``.

    Same rationale as ``_looks_like_retry_after``: the header is attacker-
    controlled on a forged response, so it is validated by shape before being
    repeated rather than trusted outright.
    """
    return bool(_REQUEST_ID_RE.match(value))


# The client-credentials flow accepts only `/.default`; a resource scope such as
# `User.Read` is rejected with AADSTS1002012.
DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Matches the Zoom client. Applies to the Graph call *and* to msal's own token
# traffic, which defaults to no timeout and would otherwise hang unbounded.
DEFAULT_TIMEOUT_SECONDS = 15

# msal caches tokens per application instance, and this plugin builds one per
# call, so its throttling protection and tenant discovery would never be reused.
# msal documents sharing a plain dict for exactly this.
_MSAL_HTTP_CACHE: dict = {}


class MSTeamsClient:
    """Minimal Microsoft Graph client for creating and deleting Teams meetings.

    Authenticates with the OAuth2 client-credentials flow, which requires the
    ``OnlineMeetings.ReadWrite.All`` application permission *and* a tenant
    application access policy naming ``user_id``. Without the policy Graph
    answers 403 even though the token is valid.
    """

    def __init__(
        self,
        client_id: str,
        authority: str,
        credential: str,
        user_id: str,
        scope: str = DEFAULT_SCOPE,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.client_id = client_id
        self.authority = authority
        self.client_credential = credential
        self.scope = scope
        self.user_id = user_id
        self.timeout = timeout
        self._app = None

    def _application(self) -> msal.ConfidentialClientApplication:
        """Build the msal application once per client.

        The constructor performs OIDC tenant discovery over the network, so it
        both needs the timeout and can raise before any request of ours is made.
        """
        if self._app is None:
            try:
                self._app = msal.ConfidentialClientApplication(
                    self.client_id,
                    authority=self.authority,
                    client_credential=self.client_credential,
                    timeout=self.timeout,
                    http_cache=_MSAL_HTTP_CACHE,
                )
            except ValueError as e:
                # msal raises a bare ValueError for an unreachable or unknown
                # authority, which reads as a config error even when it is not.
                raise DispatchPluginException(
                    f"Could not reach the Microsoft identity platform at {self.authority}. "
                    f"Check the authority URL, network egress and proxy configuration. ({e})"
                ) from e
            except requests.RequestException as e:
                raise DispatchPluginException(
                    f"Could not reach the Microsoft identity platform at {self.authority}: {e}"
                ) from e
        return self._app

    def _acquire_token(self) -> str:
        """Fetch an application token, raising with the reason if it fails."""
        try:
            # Must be a list. msal asserts `isinstance(scopes, list)` internally
            # and a bare string raises AssertionError before any request.
            result = self._application().acquire_token_for_client(scopes=[self.scope])
        except requests.RequestException as e:
            raise DispatchPluginException(f"The Microsoft Graph token request failed: {e}") from e

        if "access_token" not in result:
            # `result` also carries the raw token response; only the error
            # fields are safe to repeat.
            raise DispatchPluginException(
                "Could not acquire a Microsoft Graph access token. "
                f"{result.get('error', 'unknown_error')}: "
                f"{result.get('error_description', 'no description returned')}"
            )

        log.debug("Acquired a Microsoft Graph access token.")
        return result["access_token"]

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        token = self._acquire_token()

        try:
            response = requests.request(
                method,
                f"{GRAPH_API_BASE_URI}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
                # A same-host 307/308 would replay the POST with the auth header
                # intact and create a second meeting.
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as e:
            raise DispatchPluginException(
                f"Microsoft Graph {method} {path} could not be completed: {e}"
            ) from e

        # A delete Graph answers 404 to is reported as `ConferenceAlreadyGone`
        # rather than a failure: the meeting is not there, which is what the
        # delete wanted (issue #120). Decided here because this is the last
        # point that knows the method -- the read half of the attendee
        # read-modify-write can 404 too, and there nothing was deleted.
        if method == "DELETE" and response.status_code == 404:
            raise ConferenceAlreadyGone(
                f"Microsoft Graph {method} {path} found no such meeting (HTTP 404)."
            )

        if not 200 <= response.status_code < 300:
            raise DispatchPluginException(self._error_message(method, path, response))

        return response

    @staticmethod
    def _error_message(method: str, path: str, response: requests.Response) -> str:
        """The reason Graph gave, with nothing else from the body or headers.

        Only the structured `error.message` key is read from the body, and the
        `Retry-After`/`request-id` headers are repeated only when they have the
        shape Graph documents for them. None of this is a courtesy to the
        provider -- it is the security boundary: this request carries a live
        bearer token, and a proxy, captive portal, or WAF answering in Graph's
        place controls the whole response, headers included, and may quote the
        request back at us in any of them. That string would land on the
        incident timeline, which is broadly readable, so nothing outside these
        explicitly validated shapes is ever repeated.
        """
        try:
            detail = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = "no reason given"

        message = (
            f"Microsoft Graph {method} {path} failed with HTTP {response.status_code}: {detail}"
        )

        # Graph answers 429 with Retry-After as either a seconds count or an
        # HTTP date -- the only two shapes RFC 9110 defines for it -- so it is
        # repeated verbatim rather than given a unit. Anything else is dropped.
        retry_after = response.headers.get("Retry-After")
        if retry_after and _looks_like_retry_after(retry_after):
            message = f"{message} (Retry-After: {retry_after})"

        # The first thing a Microsoft support case asks for. Graph always
        # returns a GUID here; anything else is dropped.
        request_id = response.headers.get("request-id")
        if request_id and _looks_like_request_id(request_id):
            message = f"{message} (request-id: {request_id})"

        return message

    @staticmethod
    def _parse(response: requests.Response) -> dict:
        try:
            body = response.json()
        except ValueError as e:
            raise DispatchPluginException(
                f"Microsoft Graph returned HTTP {response.status_code} "
                "with a body that could not be parsed as JSON."
            ) from e

        if not isinstance(body, dict):
            raise DispatchPluginException(
                f"Microsoft Graph returned HTTP {response.status_code} with a "
                f"{type(body).__name__} where an object was expected."
            )

        return body

    def create_meeting(
        self,
        subject: str,
        duration_minutes: int = 60,
        record_automatically: bool = False,
        require_passcode: bool = True,
        attendees: list[dict] = None,
    ) -> dict:
        """Create an online meeting and return Graph's representation of it.

        `attendees` seeds `participants.attendees`, the same list
        `update_attendees` maintains (issue #110). The create body is a full
        onlineMeeting representation, so the roster belongs here rather than in a
        PATCH that could only run once the meeting is already committed.

        No one is emailed by this. Graph is explicit that the API "creates a
        standalone meeting that isn't associated with any event on the user's
        calendar", so there is no invitation to send. `organizer` is left out for
        the same reason `update_attendees` omits it -- Graph assigns it from the
        user in the path and rejects an attempt to set it.

        The key is omitted rather than sent empty when there is no one to list,
        which keeps the request identical to the one sent before this roster
        existed.
        """
        start = datetime.now(UTC)
        body = {
            "subject": subject,
            # Graph documents endDateTime as required on create. Sending both
            # also makes the meeting window deterministic rather than defaulted.
            "startDateTime": start.isoformat(),
            "endDateTime": (start + timedelta(minutes=duration_minutes)).isoformat(),
            "recordAutomatically": record_automatically,
            "joinMeetingIdSettings": {"isPasscodeRequired": require_passcode},
        }

        if attendees:
            body["participants"] = {"attendees": attendees}

        response = self._request(
            "POST", f"/users/{_quote_path_component(self.user_id)}/onlineMeetings", json=body
        )

        # Graph has committed the meeting by the time it answers 2xx, so a body
        # we cannot read is an orphaned bridge rather than a failed create.
        # `ConferenceCreatedButUnusable` is what gets that logged as a possible
        # leak; there is no id to delete by (issue #114). Only `create_meeting`
        # converts -- a bad body from a read or an update leaves nothing behind.
        try:
            return self._parse(response)
        except DispatchPluginException as e:
            raise ConferenceCreatedButUnusable(str(e)) from e

    def delete_meeting(self, meeting_id: str) -> None:
        """Delete an online meeting."""
        user = _quote_path_component(self.user_id)
        meeting = _quote_path_component(meeting_id)
        self._request("DELETE", f"/users/{user}/onlineMeetings/{meeting}")

    def get_meeting(self, meeting_id: str) -> dict:
        """Read an online meeting."""
        user = _quote_path_component(self.user_id)
        meeting = _quote_path_component(meeting_id)
        response = self._request("GET", f"/users/{user}/onlineMeetings/{meeting}")
        return self._parse(response)

    def update_attendees(self, meeting_id: str, attendees: list[dict]) -> dict:
        """Replace the meeting's attendee list.

        Graph has no incremental form: adjusting `attendees` "always requires the
        full list of attendees in the request body", so a caller that sends only
        the delta silently removes everyone else. `organizer` is deliberately
        absent -- it cannot be updated and sending it is rejected.
        """
        user = _quote_path_component(self.user_id)
        meeting = _quote_path_component(meeting_id)
        response = self._request(
            "PATCH",
            f"/users/{user}/onlineMeetings/{meeting}",
            json={"participants": {"attendees": attendees}},
        )
        return self._parse(response)
