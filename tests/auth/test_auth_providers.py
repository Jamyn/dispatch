"""Credential validation in the built-in authentication provider plugins.

get_current_user on these classes is the gate every authenticated API request
passes through -- DISPATCH_AUTHENTICATION_PROVIDER_SLUG defaults to
dispatch-auth-provider-basic, so BasicAuthProviderPlugin is what a default
deployment actually runs. Nothing else in the suite decodes a token.
"""

import base64
import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request

from dispatch.config import DISPATCH_JWT_ALG, DISPATCH_JWT_SECRET
from dispatch.plugins.dispatch_core.plugin import (
    AwsAlbAuthProviderPlugin,
    BasicAuthProviderPlugin,
    HeaderAuthProviderPlugin,
)


def request_with_headers(**headers) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/default/incidents",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "query_string": b"",
        }
    )


def signed_token(email="user@example.com", secret=DISPATCH_JWT_SECRET, exp_delta_seconds=3600):
    exp = (datetime.utcnow() + timedelta(seconds=exp_delta_seconds)).timestamp()
    return jwt.encode({"exp": exp, "email": email}, secret, algorithm=DISPATCH_JWT_ALG)


# --- BasicAuthProviderPlugin ---------------------------------------------


def test_a_token_this_deployment_signed_resolves_to_its_subject():
    """The success path: a token minted by DispatchUser.token identifies them."""
    request = request_with_headers(Authorization=f"Bearer {signed_token('ada@example.com')}")

    assert BasicAuthProviderPlugin().get_current_user(request) == "ada@example.com"


def test_a_token_signed_with_a_different_secret_is_rejected():
    """Forgery check: the signature, not the payload, decides identity."""
    forged = signed_token("attacker@example.com", secret="some-other-deployments-secret")
    request = request_with_headers(Authorization=f"Bearer {forged}")

    with pytest.raises(HTTPException) as exc:
        BasicAuthProviderPlugin().get_current_user(request)
    assert exc.value.status_code == 401


def test_an_expired_token_is_rejected():
    """DISPATCH_JWT_EXP has to be enforced at decode, not just at mint."""
    request = request_with_headers(Authorization=f"Bearer {signed_token(exp_delta_seconds=-60)}")

    with pytest.raises(HTTPException) as exc:
        BasicAuthProviderPlugin().get_current_user(request)
    assert exc.value.status_code == 401


def test_an_unsigned_alg_none_token_is_rejected():
    """The classic JWT bypass: a token that asserts it needs no signature.

    python-jose rejects alg=none for a keyed decode, but that is a property of
    the library rather than of this code, so it is worth pinning here -- a
    swap of the JWT library is exactly the change that would reintroduce it.
    """
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "email": "attacker@example.com",
                "exp": (datetime.utcnow() + timedelta(hours=1)).timestamp(),
            }
        ).encode()
    )
    unsigned = f"{header.decode().rstrip('=')}.{payload.decode().rstrip('=')}."
    request = request_with_headers(Authorization=f"Bearer {unsigned}")

    with pytest.raises(HTTPException) as exc:
        BasicAuthProviderPlugin().get_current_user(request)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "token abc"}],
    ids=["absent", "basic-scheme", "unknown-scheme"],
)
def test_a_non_bearer_authorization_header_yields_no_user(headers):
    """Returning None here is what makes auth.service raise 401 upstream.

    Returning a falsy-but-not-None value, or raising something other than
    HTTPException, would change that contract.
    """
    assert BasicAuthProviderPlugin().get_current_user(request_with_headers(**headers)) is None


# --- HeaderAuthProviderPlugin --------------------------------------------


def test_header_provider_returns_the_configured_header_value():
    """Trusted-header auth: whatever the proxy set is the identity."""
    request = request_with_headers(**{"remote-user": "ada@example.com"})

    assert HeaderAuthProviderPlugin().get_current_user(request) == "ada@example.com"


def test_header_provider_rejects_a_request_without_the_header():
    """A missing header must 401, never fall back to an anonymous identity."""
    with pytest.raises(HTTPException) as exc:
        HeaderAuthProviderPlugin().get_current_user(request_with_headers())
    assert exc.value.status_code == 401


# --- AwsAlbAuthProviderPlugin --------------------------------------------


def test_alb_provider_rejects_a_token_signed_by_a_different_load_balancer(monkeypatch):
    """The ARN in the header must match the configured ALB.

    Without this check any AWS account's ALB could mint an accepted identity.
    The check runs before the public key is fetched, so asserting it needs no
    network -- and get_public_key is patched to prove none was made.
    """
    import dispatch.plugins.dispatch_core.plugin as core_plugin

    monkeypatch.setattr(
        core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_ARN", "arn:aws:elb:us-east-1:1:ours"
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("public key fetched despite an ARN mismatch")

    monkeypatch.setattr(AwsAlbAuthProviderPlugin, "get_public_key", fail_if_called)

    header = base64.b64encode(
        json.dumps({"signer": "arn:aws:elb:us-east-1:2:theirs", "kid": "k1"}).encode()
    ).decode()
    request = request_with_headers(**{"x-amzn-oidc-data": f"{header}.payload.signature"})

    with pytest.raises(HTTPException) as exc:
        AwsAlbAuthProviderPlugin().get_current_user(request)
    assert exc.value.status_code == 401


def test_alb_provider_rejects_a_request_without_the_oidc_header():
    """No x-amzn-oidc-data means the request did not come through the ALB."""
    with pytest.raises(HTTPException) as exc:
        AwsAlbAuthProviderPlugin().get_current_user(request_with_headers())
    assert exc.value.status_code == 401
