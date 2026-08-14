"""Microsoft Graph client for the Teams conference plugin."""

import logging
from datetime import UTC, datetime, timedelta

import msal
import requests

from dispatch.exceptions import DispatchPluginException

log = logging.getLogger(__name__)

GRAPH_API_BASE_URI = "https://graph.microsoft.com/v1.0"

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
            raise DispatchPluginException(
                f"The Microsoft Graph token request failed: {e}"
            ) from e

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

        if not 200 <= response.status_code < 300:
            raise DispatchPluginException(self._error_message(method, path, response))

        return response

    @staticmethod
    def _error_message(method: str, path: str, response: requests.Response) -> str:
        try:
            detail = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = response.text.strip()[:200] or "no response body"

        message = (
            f"Microsoft Graph {method} {path} failed with HTTP {response.status_code}: {detail}"
        )

        # Graph answers 429 with Retry-After as either a seconds count or an
        # HTTP date, so it is repeated verbatim rather than given a unit.
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            message = f"{message} (Retry-After: {retry_after})"

        # The first thing a Microsoft support case asks for.
        request_id = response.headers.get("request-id")
        if request_id:
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
    ) -> dict:
        """Create an online meeting and return Graph's representation of it."""
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

        response = self._request("POST", f"/users/{self.user_id}/onlineMeetings", json=body)
        return self._parse(response)

    def delete_meeting(self, meeting_id: str) -> None:
        """Delete an online meeting."""
        self._request("DELETE", f"/users/{self.user_id}/onlineMeetings/{meeting_id}")
