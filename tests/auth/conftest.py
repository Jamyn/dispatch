"""Shared harness for the authorization tests.

These exercise the real chain a request goes through -- bearer token ->
BasicAuthProviderPlugin -> auth.service.get_current_user -> role lookup ->
permission class -- rather than stubbing get_current_user out. A stub would
pass whether or not the JWT is actually validated, which is the half of the
chain most worth protecting.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from dispatch.auth.models import DispatchUser
from dispatch.enums import UserRoles

from tests.factories import DispatchUserOrganizationFactory


@pytest.fixture
def basic_auth_plugin():
    """Register the provider DISPATCH_AUTHENTICATION_PROVIDER_SLUG defaults to."""
    from dispatch.plugins.base import register
    from dispatch.plugins.dispatch_core.plugin import BasicAuthProviderPlugin

    register(BasicAuthProviderPlugin)
    return BasicAuthProviderPlugin


@pytest.fixture
def as_request(session, basic_auth_plugin):
    """Build a Request that the permission classes accept.

    Permission classes read three things off a request: the organization from
    path_params, request.state.db, and the Authorization header (via the auth
    provider plugin). Anything else they touch is derived from those.
    """

    def _build(user: DispatchUser, organization, path_params=None, method="GET", body=None):
        params = {"organization": organization.slug, **(path_params or {})}
        scope = {
            "type": "http",
            "method": method,
            "path": f"/api/v1/{organization.slug}/",
            "headers": [(b"authorization", f"Bearer {user.token}".encode())],
            "path_params": params,
            "query_string": b"",
        }
        request = Request(scope)
        request.state.db = session
        request.state.organization = organization.slug
        if body is not None:
            request._body = body
        return request

    return _build


@pytest.fixture
def grant_role(session):
    """Give a user a role in an organization."""

    def _grant(user: DispatchUser, organization, role: UserRoles):
        DispatchUserOrganizationFactory(dispatch_user=user, organization=organization, role=role)
        session.commit()
        session.refresh(user)
        return user

    return _grant


@pytest.fixture
def api_client(session):
    """A TestClient over the real API.

    Local on purpose. A shared session-scoped app fixture is what makes global
    plugin-registry mutation tempting, and unregistering there strands any
    later test on the same xdist worker that resolves a plugin by slug.
    """
    from dispatch.main import api

    return TestClient(api)


@pytest.fixture
def signing_keys():
    """Two RSA keypairs published under one JWKS, as a rotation window looks."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk

    def keypair(kid):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public_pem = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        entry = {**jwk.construct(public_pem, "RS256").to_dict(), "kid": kid, "alg": "RS256"}
        # to_dict returns bytes for the modulus and exponent; the real endpoint
        # serves JSON, so hand the plugin the same str shape it would see.
        entry = {k: v.decode() if isinstance(v, bytes) else v for k, v in entry.items()}
        return pem, entry

    current_pem, current_jwk = keypair("current-key")
    next_pem, next_jwk = keypair("next-key")
    return {
        "current": (current_pem, current_jwk),
        "next": (next_pem, next_jwk),
    }
