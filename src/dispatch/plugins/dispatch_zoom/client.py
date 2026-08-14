"""Zoom API client, authenticated with Server-to-Server OAuth.

Zoom retired JWT app authorization on 2023-09-01, which is what this client used
to hand-sign (issue #70). Server-to-Server OAuth replaces it, with two details
that do not match a standard OAuth2 client-credentials flow:

- the grant type is ``account_credentials``. ``client_credentials`` is a real
  grant at this endpoint, but it belongs to General Apps; a Server-to-Server app
  is not entitled to it. (The message "This API does not support client
  credentials for authorization" is code 124 from *api.zoom.us*, returned when
  such a token is later presented -- not an error from the token endpoint.)
- the client id and secret go in an HTTP Basic header while the account id goes
  in the form body.

Tokens live an hour and there is no refresh token; a new one is simply
requested. This client keeps its token for its own lifetime and re-requests it
shortly before expiry, so the two calls a participant update makes share one.
"""

import json
import logging
import time

import requests
from requests.auth import HTTPBasicAuth

from dispatch.exceptions import DispatchPluginException

log = logging.getLogger(__name__)

API_BASE_URI = "https://api.zoom.us/v2"
OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"

DEFAULT_TIMEOUT_SECONDS = 15

# Renew this far before the stated expiry so a token cannot lapse between the
# check and the call it authenticates.
EXPIRY_MARGIN_SECONDS = 60

# Zoom documents a one-hour token. Used only as a ceiling on what it reports.
MAX_TOKEN_LIFETIME_SECONDS = 3600


class ZoomClient:
    """Simple HTTP Client for Zoom Calls."""

    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._access_token: str | None = None
        # Monotonic, so a wall-clock adjustment cannot make a live token look
        # expired or an expired one look live.
        self._expires_at = 0.0

    def _acquire_token(self) -> str:
        """Exchange the app credentials for an access token."""
        try:
            response = requests.post(
                OAUTH_TOKEN_URL,
                # Sent as a form body; `data` with a dict is urlencoded by requests.
                data={"grant_type": "account_credentials", "account_id": self.account_id},
                # Basic auth keeps the secret out of the body and the URL.
                auth=HTTPBasicAuth(self.client_id, self.client_secret),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise DispatchPluginException(
                f"The Zoom access token request could not be completed: {e}"
            ) from e

        if not 200 <= response.status_code < 300:
            raise DispatchPluginException(
                f"Could not acquire a Zoom access token. {self._token_error(response)}"
            )

        try:
            body = response.json()
        except ValueError as e:
            raise DispatchPluginException(
                f"Zoom returned HTTP {response.status_code} to the token request "
                "with a body that could not be parsed as JSON."
            ) from e

        if not isinstance(body, dict):
            raise DispatchPluginException(
                f"Zoom returned HTTP {response.status_code} to the token request with a "
                f"{type(body).__name__} where an object was expected."
            )

        token = body.get("access_token")
        if not token:
            # The body also carries the token itself; nothing from it is repeated.
            raise DispatchPluginException("Zoom returned a token response with no access_token.")

        # A response without a usable `expires_in` is treated as already expired
        # rather than as never expiring, so a surprise cannot pin a stale token
        # forever. Coerced rather than trusted, so no conversion error escapes
        # this module's DispatchPluginException contract: a string raises
        # ValueError, None raises TypeError, and 1e400 parses as float infinity
        # and raises OverflowError.
        try:
            expires_in = int(body.get("expires_in") or 0)
        except (TypeError, ValueError, OverflowError):
            expires_in = 0
        # Clamped to Zoom's documented hour so an absurd value cannot pin a token.
        expires_in = min(expires_in, MAX_TOKEN_LIFETIME_SECONDS)
        self._expires_at = time.monotonic() + max(0, expires_in - EXPIRY_MARGIN_SECONDS)

        log.debug("Acquired a Zoom access token.")
        return token

    @staticmethod
    def _token_error(response: requests.Response) -> str:
        """The reason Zoom gave, with nothing else from the body.

        Only the structured `reason`/`error` keys are read. The raw body is
        deliberately never echoed: this request carries the Basic credentials,
        and a TLS-inspecting proxy or captive portal answering in Zoom's place
        may quote the request -- headers included -- back at us. That string
        would land on the incident timeline, which is broadly readable.
        """
        try:
            body = response.json()
            detail = body.get("reason") or body.get("error") or ""
        except (ValueError, AttributeError):
            detail = ""

        return f"HTTP {response.status_code}: {str(detail).strip() or 'no reason given'}"

    def _token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        self._access_token = self._acquire_token()
        return self._access_token

    def _get_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self._token()}",
            "content-type": "application/json",
        }

    def get(self, path, params=None):
        return requests.get(
            "{}/{}".format(API_BASE_URI, path),
            params=params,
            headers=self._get_headers(),
            timeout=self.timeout,
        )

    def post(self, path, data):
        return requests.post(
            "{}/{}".format(API_BASE_URI, path),
            data=json.dumps(data),
            headers=self._get_headers(),
            timeout=self.timeout,
        )

    def patch(self, path, data):
        return requests.patch(
            "{}/{}".format(API_BASE_URI, path),
            data=json.dumps(data),
            headers=self._get_headers(),
            timeout=self.timeout,
        )

    def delete(self, path, data=None, params=None):
        return requests.delete(
            "{}/{}".format(API_BASE_URI, path),
            data=json.dumps(data),
            params=params,
            headers=self._get_headers(),
            timeout=self.timeout,
        )
