"""End-to-end authorization over real HTTP requests.

The rest of tests/auth exercises the pieces -- token decoding, role lookup,
permission classes, the query-level visibility filters -- in isolation. These
prove the pieces are actually wired together in the running app: a request
arrives, db_session_middleware resolves the tenant schema, the auth provider
resolves the caller, and the list endpoints apply the visibility rules.

tests/api/test_schema.py, the only other file under an api heading, is
commented out in full, so nothing else here goes through the ASGI stack.
"""

import pytest

from dispatch.enums import UserRoles, Visibility
from dispatch.organization import service as organization_service

from tests.factories import (
    DispatchUserFactory,
    DispatchUserOrganizationFactory,
    IncidentFactory,
    IndividualContactFactory,
    ParticipantFactory,
)


@pytest.fixture
def default_organization(session):
    return organization_service.get_by_slug(db_session=session, slug="default")


@pytest.fixture
def authenticate(session, default_organization, basic_auth_plugin):
    """Create a user with a role in the default org and return its auth header."""

    def _authenticate(role=UserRoles.member, email=None):
        user = DispatchUserFactory(**({"email": email} if email else {}))
        DispatchUserOrganizationFactory(
            dispatch_user=user, organization=default_organization, role=role
        )
        session.commit()
        return user, {"Authorization": f"Bearer {user.token}"}

    return _authenticate


# --- Tenant resolution ----------------------------------------------------


def test_a_request_for_an_unknown_organization_is_refused_by_name(client):
    """db_session_middleware maps the URL's organization onto a schema.

    Falling through to a default schema instead of refusing would serve one
    tenant's rows under another tenant's URL. The check runs ahead of
    authentication, so an unauthenticated request is enough to exercise it.
    """
    response = client.get("/no-such-org/incidents")

    assert response.status_code == 500
    assert "dispatch_organization_no-such-org" in response.json()["detail"][0]["msg"]


def test_a_request_without_credentials_is_refused(client, basic_auth_plugin):
    """No bearer token must not resolve to the default user on a real route."""
    assert client.get("/default/incidents").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer a.b.c", "Bearer ", "Basic dXNlcjpwYXNz", ""],
    ids=["garbage-token", "jwt-shaped-garbage", "empty-token", "wrong-scheme", "empty-header"],
)
def test_a_request_with_an_unusable_authorization_header_is_refused(
    client, basic_auth_plugin, header
):
    """Every malformed-credential shape has to land on 401, not 200 or 500."""
    response = client.get("/default/incidents", headers={"Authorization": header})

    assert response.status_code == 401


# --- Visibility over the wire --------------------------------------------


def test_the_incident_list_hides_restricted_incidents_from_a_non_participant(
    client, session, authenticate
):
    """The confidentiality guarantee, asserted on the response body.

    restricted_incident_filter is unit-tested in tests/database/test_service.py;
    this is what proves the API actually calls it.
    """
    _, headers = authenticate(UserRoles.member)
    open_incident = IncidentFactory(title="Open incident", visibility=Visibility.open)
    IncidentFactory(title="Restricted incident", visibility=Visibility.restricted)
    session.commit()

    body = client.get("/default/incidents", headers=headers).json()
    titles = {item["title"] for item in body["items"]}

    assert open_incident.title in titles
    assert "Restricted incident" not in titles


def test_the_incident_list_shows_a_restricted_incident_to_its_participant(
    client, session, authenticate
):
    """Participants keep access, so the filter is a filter and not a blanket."""
    user, headers = authenticate(UserRoles.member)
    restricted = IncidentFactory(title="Restricted incident", visibility=Visibility.restricted)
    restricted.participants.append(
        ParticipantFactory(individual=IndividualContactFactory(email=user.email))
    )
    session.commit()

    body = client.get("/default/incidents", headers=headers).json()

    assert "Restricted incident" in {item["title"] for item in body["items"]}


def test_the_incident_list_shows_restricted_incidents_to_an_admin(client, session, authenticate):
    """Admins see everything in their tenant -- the documented override."""
    _, headers = authenticate(UserRoles.admin)
    IncidentFactory(title="Restricted incident", visibility=Visibility.restricted)
    session.commit()

    body = client.get("/default/incidents", headers=headers).json()

    assert "Restricted incident" in {item["title"] for item in body["items"]}


def test_two_members_of_the_same_tenant_see_different_restricted_incidents(
    client, session, authenticate
):
    """The filter keys on the caller, not on a per-process or cached role.

    Both requests hit the same endpoint in the same schema in the same test,
    so a filter that ignored current_user would return identical bodies.
    """
    alice, alice_headers = authenticate(UserRoles.member, email="alice@example.com")
    _, bob_headers = authenticate(UserRoles.member, email="bob@example.com")

    alices = IncidentFactory(title="Alice's restricted", visibility=Visibility.restricted)
    alices.participants.append(
        ParticipantFactory(individual=IndividualContactFactory(email=alice.email))
    )
    IncidentFactory(title="Nobody's restricted", visibility=Visibility.restricted)
    session.commit()

    alice_titles = {
        i["title"] for i in client.get("/default/incidents", headers=alice_headers).json()["items"]
    }
    bob_titles = {
        i["title"] for i in client.get("/default/incidents", headers=bob_headers).json()["items"]
    }

    assert "Alice's restricted" in alice_titles
    assert "Alice's restricted" not in bob_titles
    assert "Nobody's restricted" not in alice_titles
    assert "Nobody's restricted" not in bob_titles


# --- Credential exposure --------------------------------------------------


def test_the_user_list_never_returns_password_hashes(client, session, authenticate):
    """UserRead is what stops the bcrypt hash reaching an API consumer.

    Every authenticated user can call this route, so a serializer change that
    leaked the column would hand every account's hash to every account.
    """
    _, headers = authenticate(UserRoles.admin)

    response = client.get("/default/users", headers=headers)
    assert response.status_code == 200

    body = response.text
    assert "password" not in body
    assert "$2b$" not in body
