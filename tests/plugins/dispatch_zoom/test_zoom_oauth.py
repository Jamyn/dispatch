"""Server-to-Server OAuth for the Zoom client (issue #70).

Zoom retired JWT app authorization on 2023-09-01, and this plugin kept
hand-signing an HS256 JWT with `iss=<api key>` long afterwards, so it could not
authenticate against live Zoom at all. These tests pin the replacement.

Two details are easy to get wrong and are asserted here rather than assumed:

- The grant type is ``account_credentials``, *not* the standard OAuth
  ``client_credentials``. Zoom's token endpoint recognises ``client_credentials``
  -- it belongs to General Apps -- but a Server-to-Server app is not entitled to
  it. Only the live suite can settle that; here it is simply pinned.
- The client id and secret travel in an HTTP Basic header, not in the form body.

Everything runs against a fake installed below ``requests``, so the real request
construction is what is being asserted -- not a stubbed helper.
"""

import base64
import logging
import time

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_zoom.conftest import (
    ACCESS_TOKEN,
    ACCOUNT_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    MEETING_ID,
    OAUTH_TOKEN_URL,
)


def build_client(**overrides):
    from dispatch.plugins.dispatch_zoom.client import ZoomClient

    kwargs = {
        "account_id": ACCOUNT_ID,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    kwargs.update(overrides)
    return ZoomClient(**kwargs)


# --- the token request itself -----------------------------------------------


def test_the_token_is_requested_from_zooms_oauth_endpoint(zoom):
    build_client().get("meetings/1")

    assert zoom.token_requests()[-1].url.split("?")[0] == OAUTH_TOKEN_URL


def test_the_token_request_is_a_post(zoom):
    build_client().get("meetings/1")

    assert zoom.token_requests()[-1].method == "POST"


def test_the_token_request_uses_the_account_credentials_grant(zoom):
    """`client_credentials` is rejected by Zoom; this is the whole point."""
    build_client().get("meetings/1")

    assert zoom.token_requests()[-1].form["grant_type"] == "account_credentials"


def test_the_token_request_carries_the_configured_account_id(zoom):
    build_client(account_id="a-different-account").get("meetings/1")

    assert zoom.token_requests()[-1].form["account_id"] == "a-different-account"


def test_the_token_request_is_form_encoded(zoom):
    """Zoom requires application/x-www-form-urlencoded, not JSON."""
    build_client().get("meetings/1")

    content_type = zoom.token_requests()[-1].headers["Content-Type"]
    assert content_type.startswith("application/x-www-form-urlencoded")


def test_the_credentials_travel_in_a_basic_auth_header(zoom):
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    build_client().get("meetings/1")

    assert zoom.token_requests()[-1].headers["Authorization"] == f"Basic {expected}"


def test_the_client_secret_is_not_in_the_token_request_body(zoom):
    """A secret in the body is a secret in every proxy log that parses forms."""
    build_client().get("meetings/1")

    assert CLIENT_SECRET not in str(zoom.token_requests()[-1].form)


def test_the_client_secret_is_never_in_a_url(zoom):
    build_client().get("meetings/1")

    for request in zoom.requests:
        assert CLIENT_SECRET not in request.url
        assert CLIENT_ID not in request.url


def test_the_token_request_has_a_timeout(zoom):
    """Without one the participant flow hangs forever on a stalled endpoint."""
    build_client().get("meetings/1")

    assert zoom.token_requests()[-1].timeout == 15


# --- the acquired token is what reaches the API -----------------------------


def test_the_api_call_carries_the_acquired_bearer_token(zoom):
    build_client().get("meetings/1")

    assert zoom.last_api_request().headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_the_token_is_acquired_before_the_api_call(zoom):
    build_client().get("meetings/1")

    assert [r.method for r in zoom.requests][0] == "POST"
    assert zoom.requests[0].url.split("?")[0] == OAUTH_TOKEN_URL


@pytest.mark.parametrize(
    "verb,args", [("get", ()), ("post", ({},)), ("patch", ({},)), ("delete", ())]
)
def test_every_verb_authenticates_with_the_bearer_token(zoom, verb, args):
    """A per-method auth path is how one verb silently keeps the old scheme."""
    getattr(build_client(), verb)("meetings/1", *args)

    assert zoom.last_api_request().headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_the_client_secret_never_reaches_the_zoom_api(zoom):
    """Only the token belongs on an API call."""
    build_client().get("meetings/1")

    request = zoom.last_api_request()
    assert CLIENT_SECRET not in str(request.headers)
    assert CLIENT_SECRET not in (request.body or "")


# --- the retired JWT flow is really gone ------------------------------------


def _zoom_plugin_modules():
    """Every module in the Zoom plugin package.

    Scoped to the package rather than to `client.py`, because JWT signing
    reintroduced one module over is still JWT signing.
    """
    import importlib
    import pkgutil

    from dispatch.plugins import dispatch_zoom

    return [
        importlib.import_module(f"{dispatch_zoom.__name__}.{info.name}")
        for info in pkgutil.iter_modules(dispatch_zoom.__path__)
    ]


def test_no_jwt_is_generated_anywhere_in_the_plugin():
    """Regression guard for issue #70.

    An implementation that adds OAuth while leaving `generate_jwt` reachable
    would pass every other test here.
    """
    for module in _zoom_plugin_modules():
        assert not hasattr(module, "generate_jwt"), f"{module.__name__} still signs a JWT"


def test_no_module_in_the_plugin_imports_a_jwt_library():
    """Asserted against the imports, not the source text.

    The module docstring explains the retired flow on purpose, so a substring
    search for "jwt" would fail on the documentation rather than on the code.

    `hmac`/`hashlib` are included because a JWT does not need a library: the old
    flow could be reconstructed in a dozen lines, and an import-name guard alone
    would not see it. The expected false positive is Zoom webhook signature
    verification, which is legitimately HMAC-SHA256 -- if that arrives, narrow
    this list rather than deleting the test.
    """
    import ast
    import inspect

    forbidden = {"jose", "jwt", "pyjwt", "authlib", "hmac", "hashlib"}

    for module in _zoom_plugin_modules():
        try:
            source = inspect.getsource(module)
        except OSError:  # pragma: no cover - every module here has source
            # Not `continue`: a guard that silently asserts nothing is worse
            # than no guard, because it still reads as coverage.
            pytest.fail(f"could not read the source of {module.__name__} to check its imports")

        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                imported.update(alias.name for alias in node.names)

        offenders = sorted(imported & forbidden)
        assert not offenders, f"{module.__name__} imports {offenders}"


def test_no_credential_travels_outside_the_bearer_header(zoom):
    """The old flow put a self-signed token on the request; nothing else may.

    Distinct from asserting the bearer value: this catches a JWT smuggled
    alongside a valid OAuth token, in a second header or the query string.
    """
    build_client().get("meetings/1")

    request = zoom.last_api_request()
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"

    other_auth = {
        name: value
        for name, value in request.headers.items()
        if name.lower() != "authorization"
        and any(hint in name.lower() for hint in ("auth", "token", "jwt", "key", "secret"))
    }
    assert not other_auth, f"unexpected credential headers: {sorted(other_auth)}"
    assert "?" not in request.url, "no credential belongs in the query string"


def test_the_client_no_longer_accepts_api_key_and_secret():
    """A caller left on the old signature must fail loudly, not silently."""
    from dispatch.plugins.dispatch_zoom.client import ZoomClient

    # Positionally, the old two-argument call.
    with pytest.raises(TypeError):
        ZoomClient("an-api-key", "an-api-secret")

    # By keyword, which arity alone would not catch.
    with pytest.raises(TypeError):
        ZoomClient(api_key="an-api-key", api_secret="an-api-secret")

    # And the old three-argument shape, which has the same arity as the new one.
    with pytest.raises(TypeError):
        ZoomClient(api_user_id="u@example.com", api_key="k", api_secret="s")


# --- token reuse and expiry -------------------------------------------------


def test_the_token_is_reused_across_calls_on_one_client(zoom):
    """add/remove_participant makes two API calls; one token should serve both."""
    client = build_client()
    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before


def test_an_expired_token_is_replaced(zoom):
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "token_type": "bearer", "expires_in": 0})
    client = build_client()

    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before + 1


def test_a_token_near_expiry_is_replaced_rather_than_used(zoom):
    """A token valid for another second expires mid-flight against real Zoom."""
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "token_type": "bearer", "expires_in": 30})
    client = build_client()

    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before + 1


def test_a_fresh_token_is_not_re_requested(zoom):
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "token_type": "bearer", "expires_in": 3600})
    client = build_client()

    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before


def test_two_clients_do_not_share_a_token(zoom):
    """Caching per instance, not per process -- no cross-tenant leakage."""
    build_client().get("meetings/1")
    before = len(zoom.token_requests())
    build_client().get("meetings/1")

    assert len(zoom.token_requests()) == before + 1


@pytest.mark.parametrize(
    "expires_in",
    [
        "not-a-number",  # ValueError
        None,  # TypeError
        1e400,  # parses as float infinity -> OverflowError
        -1,
        0,
        [],
        {"nested": "object"},
    ],
)
def test_an_unusable_expiry_does_not_escape_as_a_conversion_error(zoom, expires_in):
    """Every failure in this client is a DispatchPluginException, or it is a crash.

    A string `expires_in` used to reach `int - int` arithmetic directly. The
    float-infinity case is the one a `(TypeError, ValueError)` guard misses.
    """
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "expires_in": expires_in})

    # The call must succeed: an unusable expiry is not a reason to fail a request.
    build_client().get("meetings/1")


def test_an_unusable_expiry_is_treated_as_already_expired(zoom):
    """Fail closed: an expiry that cannot be read must not pin the token."""
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "expires_in": "not-a-number"})
    client = build_client()

    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before + 1


def test_a_numeric_string_expiry_is_honoured_rather_than_merely_survived(zoom):
    """Coercion must actually parse, not fall through to the zero default.

    Without this, `except (TypeError, ValueError): expires_in = 0` written as an
    unconditional `expires_in = 0` would still pass every other expiry test.
    """
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "expires_in": "3600"})
    client = build_client()

    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before, "the coerced expiry was not honoured"


def test_an_absurd_expiry_is_clamped_to_zooms_documented_hour(zoom):
    """A token cannot be pinned for longer than Zoom would ever issue one."""
    from dispatch.plugins.dispatch_zoom.client import (
        EXPIRY_MARGIN_SECONDS,
        MAX_TOKEN_LIFETIME_SECONDS,
    )

    zoom.token = (200, {"access_token": ACCESS_TOKEN, "expires_in": 10**9})
    client = build_client()
    client.get("meetings/1")

    remaining = client._expires_at - time.monotonic()
    assert remaining <= MAX_TOKEN_LIFETIME_SECONDS - EXPIRY_MARGIN_SECONDS + 1


def test_a_token_response_without_an_expiry_is_not_reused_forever(zoom):
    """Zoom always sends expires_in; a missing one must not mean 'never expires'."""
    zoom.token = (200, {"access_token": ACCESS_TOKEN, "token_type": "bearer"})
    client = build_client()

    client.get("meetings/1")
    before = len(zoom.token_requests())
    client.get("meetings/1")

    assert len(zoom.token_requests()) == before + 1


# --- token failures ---------------------------------------------------------


def test_invalid_credentials_raise_with_the_reason(zoom):
    zoom.token = (401, {"reason": "Invalid client_id or client_secret", "error": "invalid_client"})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "Invalid client_id or client_secret" in str(excinfo.value)


def test_a_rejected_grant_type_is_reported(zoom):
    """Zoom's real wording when the token endpoint refuses a grant.

    This asserts the reporting path, not the regression: a fake cannot prove
    which grants Zoom accepts, so only `test_zoom_live.py` settles that.
    """
    zoom.token = (
        400,
        {
            "reason": "grant type client_credentials is not supported from token endpoint",
            "error": "unsupported_grant_type",
        },
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "not supported from token endpoint" in str(excinfo.value)


def test_the_error_key_is_used_when_no_reason_is_given(zoom):
    """Zoom sends `reason` and `error`; only the second is guaranteed."""
    zoom.token = (400, {"error": "invalid_client"})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "invalid_client" in str(excinfo.value)


def test_a_failure_with_no_recognisable_detail_still_reports_the_status(zoom):
    zoom.token = (503, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    message = str(excinfo.value)
    assert "503" in message and "no reason given" in message


def test_a_failed_token_request_does_not_reach_the_api(zoom):
    zoom.token = (401, {"reason": "Invalid client_id or client_secret"})

    with pytest.raises(DispatchPluginException):
        build_client().get("meetings/1")

    assert zoom.api_requests() == []


def test_a_token_response_without_an_access_token_raises(zoom):
    zoom.token = (200, {"token_type": "bearer", "expires_in": 3599})

    with pytest.raises(DispatchPluginException):
        build_client().get("meetings/1")


def test_a_malformed_token_response_raises(zoom):
    zoom.token = (200, "not-an-object")

    with pytest.raises(DispatchPluginException):
        build_client().get("meetings/1")


def test_a_non_json_error_body_raises_cleanly(zoom):
    """A gateway answering in Zoom's place, in HTML.

    Sent as `bytes`, which the fake returns verbatim -- a `str` would be
    json-encoded into a valid JSON string and exercise no parse failure at all.
    """
    zoom.token = (502, b"<html>Bad Gateway</html>")

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "502" in str(excinfo.value)


def test_a_successful_but_non_json_token_response_raises_cleanly(zoom):
    """The 2xx path, which the 502 case short-circuits before ever parsing.

    This is what covers `_acquire_token`'s JSON guard; without it that whole
    branch can be deleted with the suite still green.
    """
    zoom.token = (200, b"<html>Sign in to your proxy</html>")

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "not be parsed as JSON" in str(excinfo.value)


def test_a_non_json_error_body_is_not_echoed_into_the_message(zoom):
    """The token request carries the Basic header; an intermediary may quote it.

    Whatever a middlebox says goes onto the incident timeline if it is echoed,
    so only Zoom's structured `reason`/`error` keys are ever repeated.
    """
    zoom.token = (403, b"Blocked. Authorization: Basic dGVzdC1jbGllbnQtaWQ6c2VjcmV0")

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    message = str(excinfo.value)
    assert "Basic" not in message
    assert "dGVzdC1jbGllbnQtaWQ6c2VjcmV0" not in message


def test_a_network_failure_on_the_token_request_raises(zoom, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    def explode(self, request, **kwargs):
        raise requests.exceptions.ConnectTimeout("token endpoint timed out")

    monkeypatch.setattr(HTTPAdapter, "send", explode)

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "timed out" in str(excinfo.value)


def test_an_insufficient_scope_error_is_surfaced(zoom):
    zoom.token = (400, {"reason": "Invalid scope", "error": "invalid_scope"})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert "Invalid scope" in str(excinfo.value)


# --- secrets stay out of the output -----------------------------------------


def test_the_client_secret_is_never_logged(zoom, caplog):
    with caplog.at_level(logging.DEBUG):
        build_client().get("meetings/1")

    assert CLIENT_SECRET not in caplog.text


def test_the_access_token_is_never_logged(zoom, caplog):
    with caplog.at_level(logging.DEBUG):
        build_client().get("meetings/1")

    assert ACCESS_TOKEN not in caplog.text


def test_the_client_secret_is_not_in_a_token_failure_message(zoom):
    """Plugin exceptions reach the incident timeline, which is exported."""
    zoom.token = (401, {"reason": "Invalid client_id or client_secret"})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().get("meetings/1")

    assert CLIENT_SECRET not in str(excinfo.value)


def test_the_access_token_is_not_in_an_api_failure_message(zoom, zoom_plugin):
    zoom.get = (403, {"message": "Forbidden"})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert ACCESS_TOKEN not in str(excinfo.value)


def test_the_authorization_header_is_not_logged(zoom, caplog):
    with caplog.at_level(logging.DEBUG):
        build_client().get("meetings/1")

    assert "Basic " not in caplog.text
    assert "Bearer " not in caplog.text


# --- the plugin wires the configuration through -----------------------------


def test_the_plugin_sends_the_configured_account_id(zoom, zoom_plugin):
    zoom_plugin.configuration.account_id = "the-configured-account"
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.token_requests()[-1].form["account_id"] == "the-configured-account"


def test_the_plugin_sends_the_configured_client_credentials(zoom, zoom_plugin):
    """Mis-plumbing these leaves the plugin dead against a real account."""
    from pydantic import SecretStr

    zoom_plugin.configuration.client_id = "the-configured-client"
    zoom_plugin.configuration.client_secret = SecretStr("the-configured-secret")
    zoom_plugin.create("dispatch-incident-1")

    expected = base64.b64encode(b"the-configured-client:the-configured-secret").decode()
    assert zoom.token_requests()[-1].headers["Authorization"] == f"Basic {expected}"


def test_the_account_id_and_client_id_are_not_confused(zoom, zoom_plugin):
    """Both are opaque strings, so a swap is invisible without asserting both."""
    zoom_plugin.create("dispatch-incident-1")

    request = zoom.token_requests()[-1]
    assert request.form["account_id"] == ACCOUNT_ID
    expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
