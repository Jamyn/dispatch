"""The whole Entra sign-in path, over real HTTP, against a stand-in issuer.

tests/auth/test_oidc_entra.py exercises the provider directly with the key
endpoint patched out. This runs a real HTTP server that serves a JWKS the way
`login.microsoftonline.com/<tenant>/discovery/v2.0/keys` does, and drives an
Entra-shaped id token through the running application: ASGI request ->
db_session_middleware -> PKCE provider -> JWKS fetch over the network -> user
provisioning -> permission class -> route.

Nothing here needs an Entra tenant. What the stand-in cannot prove is the
browser half of the flow -- the authorization request, PKCE exchange and SPA
CORS behaviour that only Entra itself implements.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from jose import jwt

import dispatch.plugins.dispatch_core.plugin as core_plugin
from dispatch.plugins.dispatch_core.plugin import PKCEAuthProviderPlugin

from tests.auth.test_oidc_entra import (
    OTHER_TENANT_ID,
    TENANT_ID,
    UPN,
    entra_claims,
)


class _KeysHandler(BaseHTTPRequestHandler):
    """Serves one tenant's key set, and 404s anything else."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path != f"/{TENANT_ID}/discovery/v2.0/keys":
            self.send_error(404)
            return
        body = json.dumps({"keys": self.server.keys}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def entra_issuer(signing_keys):
    """A stand-in for the tenant's OpenID Connect key endpoint."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeysHandler)
    server.keys = [signing_keys["current"][1]]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    server.jwks_url = f"http://{host}:{port}/{TENANT_ID}/discovery/v2.0/keys"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def entra_deployment(entra_issuer, monkeypatch, session):
    """Configure the app exactly as the Entra documentation says to."""
    from dispatch.auth import service as auth_service
    from dispatch.plugins.base import register

    issuer = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
    client_id = "11112222-bbbb-3333-cccc-4444dddd5555"

    register(PKCEAuthProviderPlugin)
    monkeypatch.setattr(
        auth_service, "DISPATCH_AUTHENTICATION_PROVIDER_SLUG", "dispatch-auth-provider-pkce"
    )
    monkeypatch.setattr(
        core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS", entra_issuer.jwks_url
    )
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_ISSUER", issuer)
    monkeypatch.setattr(core_plugin, "DISPATCH_JWT_AUDIENCE", client_id)
    monkeypatch.setattr(core_plugin, "DISPATCH_JWT_EMAIL_OVERRIDE", "preferred_username")
    core_plugin.jwks_cache.clear()
    yield entra_issuer
    core_plugin.jwks_cache.clear()


def auth_header(signing_keys, **claim_overrides) -> dict:
    pem, entry = signing_keys["current"]
    token = jwt.encode(
        entra_claims(**claim_overrides), pem, algorithm="RS256", headers={"kid": entry["kid"]}
    )
    return {"Authorization": f"Bearer {token}"}


def test_an_entra_user_can_call_the_api_and_is_provisioned_on_the_way(
    entra_deployment, api_client, signing_keys, session
):
    """The success path, end to end and over the wire."""
    from dispatch.auth import service as auth_service
    from dispatch.enums import UserRoles

    response = api_client.get("/default/incidents", headers=auth_header(signing_keys))

    assert response.status_code == 200

    user = auth_service.get_by_email(db_session=session, email=UPN)
    assert user is not None
    assert user.get_organization_role("default") == UserRoles.member


def test_a_foreign_tenants_token_is_refused_by_the_running_app(
    entra_deployment, api_client, signing_keys, session
):
    """Signed by the trusted key, rejected on its issuer, and no account left behind."""
    from dispatch.auth import service as auth_service

    response = api_client.get(
        "/default/incidents",
        headers=auth_header(
            signing_keys, iss=f"https://login.microsoftonline.com/{OTHER_TENANT_ID}/v2.0"
        ),
    )

    assert response.status_code == 401
    assert auth_service.get_by_email(db_session=session, email=UPN) is None


def test_an_unreachable_key_endpoint_refuses_rather_than_erroring(
    entra_deployment, api_client, signing_keys
):
    """A provider outage with a cold cache must be a 401, not a 500."""
    entra_deployment.shutdown()

    response = api_client.get("/default/incidents", headers=auth_header(signing_keys))

    assert response.status_code == 401
