"""
.. module: dispatch.plugins.dispatch_microsoft_teams.conference.client
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
"""

import logging
from datetime import UTC, datetime, timedelta

import msal
import requests

from dispatch.exceptions import DispatchPluginException

logger = logging.getLogger(__name__)

GRAPH_API_BASE_URI = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Matches the Zoom client. Without one, a stalled Graph call blocks incident
# creation indefinitely.
DEFAULT_TIMEOUT_SECONDS = 15


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
        record_automatically: bool,
        user_id: str,
        scope: str = DEFAULT_SCOPE,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.client_id = client_id
        self.authority = authority
        self.client_credential = credential
        self.scope = scope
        self.record_automatically = record_automatically
        self.user_id = user_id
        self.timeout = timeout

    def _acquire_token(self) -> str:
        """Fetch an application token, raising with the reason if it fails."""
        app = msal.ConfidentialClientApplication(
            self.client_id, authority=self.authority, client_credential=self.client_credential
        )
        # Must be a list. msal asserts `isinstance(scopes, list)` internally and
        # a bare string raises AssertionError before any request is made.
        result = app.acquire_token_for_client(scopes=[self.scope])

        if "access_token" not in result:
            # `result` also carries the raw token response; only the error
            # fields are safe to repeat.
            raise DispatchPluginException(
                "Could not acquire a Microsoft Graph access token. "
                f"{result.get('error', 'unknown_error')}: "
                f"{result.get('error_description', 'no description returned')}"
            )

        logger.debug("Acquired a Microsoft Graph access token.")
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
                **kwargs,
            )
        except requests.RequestException as e:
            raise DispatchPluginException(
                f"Microsoft Graph {method} {path} could not be completed: {e}"
            ) from e

        if not response.ok:
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

        # Graph throttles /onlineMeetings at 4 requests/second per app per
        # tenant and answers 429 with a Retry-After the operator needs to see.
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            message = f"{message} (Retry-After: {retry_after}s)"

        return message

    @staticmethod
    def _parse(response: requests.Response) -> dict:
        try:
            return response.json()
        except ValueError as e:
            raise DispatchPluginException(
                f"Microsoft Graph returned HTTP {response.status_code} "
                "with a body that could not be parsed as JSON."
            ) from e

    def create_meeting(
        self,
        subject: str,
        duration_minutes: int = 60,
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
            "recordAutomatically": self.record_automatically,
            "joinMeetingIdSettings": {"isPasscodeRequired": require_passcode},
        }

        response = self._request("POST", f"/users/{self.user_id}/onlineMeetings", json=body)
        return self._parse(response)

    def delete_meeting(self, meeting_id: str) -> None:
        """Delete an online meeting."""
        self._request("DELETE", f"/users/{self.user_id}/onlineMeetings/{meeting_id}")
