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
