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
    PKCEAuthProviderPlugin,
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


def test_a_token_signed_with_an_unexpected_algorithm_is_rejected():
    """The decode pins DISPATCH_JWT_ALG rather than trusting the token's header.

    Left open, python-jose verifies with whatever algorithm the token asks
    for. That is the precondition for algorithm confusion, so the pin is
    asserted here even though this provider's shared secret makes the HS256
    case unexploitable on its own.
    """
    exp = (datetime.utcnow() + timedelta(hours=1)).timestamp()
    other_alg = jwt.encode(
        {"exp": exp, "email": "attacker@example.com"}, DISPATCH_JWT_SECRET, algorithm="HS512"
    )
    request = request_with_headers(Authorization=f"Bearer {other_alg}")

    with pytest.raises(HTTPException) as exc:
        BasicAuthProviderPlugin().get_current_user(request)
    assert exc.value.status_code == 401


def test_a_bearer_scheme_with_an_empty_token_yields_no_user():
    """ "Authorization: Bearer " once raised IndexError -- an unauthenticated 500.

    It has to take the same path as any other unusable header: return None so
    auth.service raises the 401.
    """
    request = request_with_headers(Authorization="Bearer ")

    assert BasicAuthProviderPlugin().get_current_user(request) is None


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


# --- PKCEAuthProviderPlugin ----------------------------------------------


@pytest.fixture
def rsa_keypair():
    """An RSA keypair plus the JWKS entry an identity provider would publish."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    jwks_entry = {**jwk.construct(public_pem, "RS256").to_dict(), "kid": "test-key"}
    # to_dict returns bytes for the modulus and exponent; the real endpoint
    # serves JSON, so hand the plugin the same str shape it would see there.
    jwks_entry = {k: v.decode() if isinstance(v, bytes) else v for k, v in jwks_entry.items()}
    return private_pem, public_pem, jwks_entry


@pytest.fixture
def published_jwks(monkeypatch, rsa_keypair):
    """Serve the keypair's public half where the plugin fetches its JWKS."""
    import dispatch.plugins.dispatch_core.plugin as core_plugin

    _, _, jwks_entry = rsa_keypair

    class FakeResponse:
        @staticmethod
        def json():
            return {"keys": [jwks_entry]}

    monkeypatch.setattr(core_plugin.requests, "get", lambda *a, **kw: FakeResponse())
    return jwks_entry


def pkce_request(token: str) -> Request:
    return request_with_headers(Authorization=f"Bearer {token}")


def test_pkce_accepts_a_token_signed_by_the_published_key(rsa_keypair, published_jwks):
    """The success path: an RS256 token whose kid matches the JWKS."""
    private_pem, _, _ = rsa_keypair
    token = jwt.encode(
        {"email": "ada@example.com"}, private_pem, algorithm="RS256", headers={"kid": "test-key"}
    )

    assert PKCEAuthProviderPlugin().get_current_user(pkce_request(token)) == "ada@example.com"


def test_pkce_rejects_a_token_signed_with_the_public_key_as_an_hmac_secret(
    rsa_keypair, published_jwks
):
    """The algorithm-confusion shape: HS256 signed with the public key.

    Unpinned, python-jose does block this -- but via a JWK key-type check that
    raises JWKError, which the handler's `except JWTError` does not catch, so
    it escapes as a 500. Pinning turns it into a clean 401 and stops the
    rejection depending on library internals. The token is assembled by hand
    because python-jose refuses to mint one; an attacker is not so limited.
    """
    import hashlib
    import hmac

    _, public_pem, _ = rsa_keypair

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "test-key"}).encode())
    payload = b64(json.dumps({"email": "attacker@example.com"}).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = b64(hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest())
    forged = f"{header}.{payload}.{signature}"

    with pytest.raises(HTTPException) as exc:
        PKCEAuthProviderPlugin().get_current_user(pkce_request(forged))
    assert exc.value.status_code == 401


def test_pkce_rejects_a_token_signed_by_a_key_the_provider_does_not_publish(
    published_jwks,
):
    """A different private key, announced under the published kid."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_pem = attacker_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    forged = jwt.encode(
        {"email": "attacker@example.com"},
        attacker_pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    with pytest.raises(HTTPException) as exc:
        PKCEAuthProviderPlugin().get_current_user(pkce_request(forged))
    assert exc.value.status_code == 401


def test_pkce_rejects_a_token_whose_kid_matches_no_published_key(rsa_keypair, published_jwks):
    """An unmatched kid must 401, not dereference an unbound key.

    Key rotation makes this reachable in normal operation: a token minted
    against a key the endpoint has since dropped arrives with a stale kid.
    """
    private_pem, _, _ = rsa_keypair
    token = jwt.encode(
        {"email": "ada@example.com"}, private_pem, algorithm="RS256", headers={"kid": "rotated-out"}
    )

    with pytest.raises(HTTPException) as exc:
        PKCEAuthProviderPlugin().get_current_user(pkce_request(token))
    assert exc.value.status_code == 401
