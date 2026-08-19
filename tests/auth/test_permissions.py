"""Authorization contracts enforced by dispatch.auth.permissions.

Every write endpoint in the API guards itself with one of these classes, so a
regression here is an access-control regression rather than a broken feature.
The query-layer half of the same defence (restricted_incident_filter and
friends) is covered in tests/database/test_service.py; these cover the
per-object half.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from dispatch.auth.permissions import (
    CaseEditPermission,
    CaseJoinPermission,
    CaseParticipantPermission,
    CaseViewPermission,
    FeedbackDeletePermission,
    IncidentCommanderPermission,
    IncidentEditPermission,
    IncidentEventPermission,
    IncidentJoinOrSubscribePermission,
    IncidentReporterPermission,
    IncidentTaskCreateEditPermission,
    IncidentViewPermission,
    IndividualContactUpdatePermission,
    OrganizationAdminPermission,
    OrganizationManagerPermission,
    OrganizationMemberPermission,
    OrganizationOwnerPermission,
    SensitiveProjectActionPermission,
)
from dispatch.enums import UserRoles, Visibility
from dispatch.participant_role.enums import ParticipantRoleType

from tests.factories import (
    CaseFactory,
    DispatchUserFactory,
    IncidentFactory,
    IndividualContactFactory,
    ParticipantFactory,
    ParticipantRoleFactory,
)


def allows(permission_class, request) -> bool:
    """True if the permission grants access, False if it raises 403/404."""
    try:
        permission_class(request=request)
    except HTTPException:
        return False
    return True


# --- Organization scoping -------------------------------------------------


def test_permission_rejects_an_organization_the_caller_is_not_a_member_of(
    session, user, organizations, grant_role, as_request
):
    """A role in one organization must not carry into another.

    Dispatch is multi-tenant on a shared database; the only thing separating
    two tenants at this layer is that the role lookup is scoped by slug.
    """
    home, other = organizations
    grant_role(user, home, UserRoles.owner)

    assert allows(OrganizationOwnerPermission, as_request(user, home))
    assert not allows(OrganizationOwnerPermission, as_request(user, other))


def test_an_unknown_organization_slug_never_resolves_to_a_grant(
    session, user, organization, grant_role, as_request
):
    """A slug with no organization behind it must raise, not fall through.

    get_by_slug_or_raise signals this with a pydantic ValidationError, which
    the app's ExceptionMiddleware renders as a 422 rather than the 404 the
    class's own org_error_code suggests. What matters here is that the caller
    is refused; the status code is asserted end-to-end in
    test_api_authorization.py instead.
    """
    grant_role(user, organization, UserRoles.owner)
    request = as_request(user, organization, path_params={"organization": "no-such-org"})

    with pytest.raises(ValidationError):
        OrganizationOwnerPermission(request=request)


# --- Role hierarchy -------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected",
    [
        (UserRoles.owner, {"owner", "manager", "admin", "member", "sensitive"}),
        (UserRoles.manager, {"manager", "admin", "member", "sensitive"}),
        (UserRoles.admin, {"admin", "member", "sensitive"}),
        (UserRoles.member, {"member"}),
    ],
)
def test_organization_roles_escalate_downward_only(
    session, organization, grant_role, as_request, role, expected
):
    """Owner > manager > admin > member, and never the other way around.

    Asserted as a whole table because the classes implement the hierarchy by
    each delegating to the tier above via any_permission -- one broken link
    silently promotes every role below it.
    """
    user = DispatchUserFactory()
    grant_role(user, organization, role)
    request = as_request(user, organization)

    granted = {
        name
        for name, cls in (
            ("owner", OrganizationOwnerPermission),
            ("manager", OrganizationManagerPermission),
            ("admin", OrganizationAdminPermission),
            ("member", OrganizationMemberPermission),
            ("sensitive", SensitiveProjectActionPermission),
        )
        if allows(cls, request)
    }

    assert granted == expected


def test_a_user_with_no_role_in_the_organization_is_denied(session, user, organization, as_request):
    """No membership row means no role, which must not read as a grant."""
    request = as_request(user, organization)

    assert not allows(OrganizationMemberPermission, request)
    assert not allows(SensitiveProjectActionPermission, request)


# --- Restricted incident visibility --------------------------------------


def test_restricted_incident_is_hidden_from_a_member_who_is_not_a_participant(
    session, organization, grant_role, as_request
):
    """The core confidentiality guarantee for restricted incidents."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    incident = IncidentFactory(visibility=Visibility.restricted)
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert not allows(IncidentViewPermission, request)


def test_restricted_incident_is_visible_to_a_participant(
    session, organization, grant_role, as_request
):
    """A participant on a restricted incident keeps access to it."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    incident = IncidentFactory(visibility=Visibility.restricted)
    incident.participants.append(
        ParticipantFactory(individual=IndividualContactFactory(email=user.email))
    )
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert allows(IncidentViewPermission, request)


def test_restricted_incident_is_visible_to_an_organization_admin(
    session, organization, grant_role, as_request
):
    """Admins are the documented override for restricted visibility."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.admin)
    incident = IncidentFactory(visibility=Visibility.restricted)
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert allows(IncidentViewPermission, request)


def test_open_incident_is_visible_without_any_organization_role(
    session, user, organization, as_request
):
    """Open is open: visibility gating must not become a second role check."""
    incident = IncidentFactory(visibility=Visibility.open)
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert allows(IncidentViewPermission, request)


def test_view_permission_denies_an_incident_that_does_not_exist(
    session, organization, grant_role, as_request
):
    """A missing object is a denial, not an unhandled AttributeError."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": 999999})

    assert not allows(IncidentViewPermission, request)


# --- Self-join gating -----------------------------------------------------


def test_self_join_is_refused_on_a_restricted_incident(
    session, organization, grant_role, as_request
):
    """Restricted incidents must not be joinable by their own audience."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    incident = IncidentFactory(visibility=Visibility.restricted)
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert not allows(IncidentJoinOrSubscribePermission, request)


def test_self_join_is_refused_when_the_project_disables_it(
    session, organization, grant_role, as_request
):
    """project.allow_self_join is a real gate, not advisory UI state."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    incident = IncidentFactory(visibility=Visibility.open)
    incident.project.allow_self_join = False
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert not allows(IncidentJoinOrSubscribePermission, request)


def test_an_admin_overrides_a_project_that_disables_self_join(
    session, organization, grant_role, as_request
):
    """The documented escape hatch for allow_self_join is the admin role."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.admin)
    incident = IncidentFactory(visibility=Visibility.open)
    incident.project.allow_self_join = False
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert allows(IncidentJoinOrSubscribePermission, request)


def test_self_join_is_allowed_on_an_open_incident_in_a_permissive_project(
    session, user, organization, as_request
):
    """The default path stays open, so the gates above are gates not walls."""
    incident = IncidentFactory(visibility=Visibility.open)
    incident.project.allow_self_join = True
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert allows(IncidentJoinOrSubscribePermission, request)


# --- Incident edit identity ----------------------------------------------


def test_incident_edit_is_granted_to_the_commander_and_refused_to_a_bystander(
    session, organization, grant_role, as_request
):
    """Edit rights follow incident role, independent of organization role."""
    commander_user = DispatchUserFactory()
    bystander = DispatchUserFactory()
    grant_role(commander_user, organization, UserRoles.member)
    grant_role(bystander, organization, UserRoles.member)

    incident = IncidentFactory(visibility=Visibility.open)
    incident.commander = ParticipantFactory(
        individual=IndividualContactFactory(email=commander_user.email)
    )
    session.commit()

    params = {"incident_id": incident.id}
    assert allows(IncidentCommanderPermission, as_request(commander_user, organization, params))
    assert allows(IncidentEditPermission, as_request(commander_user, organization, params))
    assert not allows(IncidentCommanderPermission, as_request(bystander, organization, params))
    assert not allows(IncidentEditPermission, as_request(bystander, organization, params))


def test_incident_edit_is_granted_to_the_reporter(session, organization, grant_role, as_request):
    """The reporter keeps edit rights on what they filed."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)

    incident = IncidentFactory(visibility=Visibility.open)
    incident.reporter = ParticipantFactory(individual=IndividualContactFactory(email=user.email))
    session.commit()

    request = as_request(user, organization, path_params={"incident_id": incident.id})

    assert allows(IncidentReporterPermission, request)
    assert allows(IncidentEditPermission, request)


# --- Task permissions derived from the request body ----------------------


def test_task_create_derives_its_incident_from_the_request_body(
    session, organization, grant_role, as_request
):
    """POST /tasks carries the incident in its body, not its path.

    The class reads request._body to find it, so a body that does not name an
    incident the caller may edit must be refused rather than defaulted.
    """
    commander_user = DispatchUserFactory()
    bystander = DispatchUserFactory()
    grant_role(commander_user, organization, UserRoles.member)
    grant_role(bystander, organization, UserRoles.member)

    incident = IncidentFactory(visibility=Visibility.open)
    incident.commander = ParticipantFactory(
        individual=IndividualContactFactory(email=commander_user.email)
    )
    session.commit()

    body = f'{{"incident": {{"id": {incident.id}}}}}'.encode()

    assert allows(
        IncidentTaskCreateEditPermission,
        as_request(commander_user, organization, method="POST", body=body),
    )
    assert not allows(
        IncidentTaskCreateEditPermission,
        as_request(bystander, organization, method="POST", body=body),
    )


@pytest.mark.parametrize(
    "body", [b"not json at all", b"{}", b'{"incident": {}}'], ids=["malformed", "empty", "no-id"]
)
def test_task_create_refuses_a_body_that_does_not_name_an_incident(
    session, organization, grant_role, as_request, body
):
    """Failing to parse the body must deny, never fall through to a grant."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    session.commit()

    request = as_request(user, organization, method="POST", body=body)

    assert not allows(IncidentTaskCreateEditPermission, request)


# --- Cases ----------------------------------------------------------------


def test_restricted_case_is_hidden_from_a_non_participant_and_shown_to_a_participant(
    session, organization, grant_role, as_request
):
    """Cases carry the same confidentiality contract as incidents."""
    outsider = DispatchUserFactory()
    insider = DispatchUserFactory()
    grant_role(outsider, organization, UserRoles.member)
    grant_role(insider, organization, UserRoles.member)

    case = CaseFactory(visibility=Visibility.restricted)
    case.participants.append(
        ParticipantFactory(individual=IndividualContactFactory(email=insider.email))
    )
    session.commit()

    params = {"case_id": case.id}
    assert not allows(CaseViewPermission, as_request(outsider, organization, params))
    assert allows(CaseViewPermission, as_request(insider, organization, params))
    assert allows(CaseParticipantPermission, as_request(insider, organization, params))


def test_case_edit_is_granted_to_the_assignee_and_refused_to_a_bystander(
    session, organization, grant_role, as_request
):
    """Case edit follows assignee/reporter identity, like incidents do."""
    assignee_user = DispatchUserFactory()
    bystander = DispatchUserFactory()
    grant_role(assignee_user, organization, UserRoles.member)
    grant_role(bystander, organization, UserRoles.member)

    case = CaseFactory(visibility=Visibility.open)
    case.assignee = ParticipantFactory(
        individual=IndividualContactFactory(email=assignee_user.email)
    )
    session.commit()

    params = {"case_id": case.id}
    assert allows(CaseEditPermission, as_request(assignee_user, organization, params))
    assert not allows(CaseEditPermission, as_request(bystander, organization, params))


def test_case_self_join_is_refused_when_restricted_or_disabled_by_the_project(
    session, organization, grant_role, as_request
):
    """Both case join gates, asserted against the same permissive baseline."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)

    open_case = CaseFactory(visibility=Visibility.open)
    open_case.project.allow_self_join = True
    restricted_case = CaseFactory(visibility=Visibility.restricted)
    closed_project_case = CaseFactory(visibility=Visibility.open)
    closed_project_case.project.allow_self_join = False
    session.commit()

    def join(case):
        return allows(
            CaseJoinPermission, as_request(user, organization, path_params={"case_id": case.id})
        )

    assert join(open_case)
    assert not join(restricted_case)
    assert not join(closed_project_case)


# --- Feedback ------------------------------------------------------------


def test_anonymous_feedback_cannot_be_deleted_by_an_unprivileged_user(
    session, organization, grant_role, as_request
):
    """individual_contact_id "0" marks anonymous feedback.

    There is no owner to match against, so the only remaining route to a grant
    is the sensitive-action role -- the sentinel must not short-circuit to one.
    """
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    session.commit()

    request = as_request(user, organization, path_params={"individual_contact_id": "0"})

    assert not allows(FeedbackDeletePermission, request)


def test_feedback_is_deletable_by_the_individual_who_left_it(
    session, organization, grant_role, as_request
):
    """A member may delete their own feedback without an admin role."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    contact = IndividualContactFactory(email=user.email)
    session.commit()

    request = as_request(user, organization, path_params={"individual_contact_id": str(contact.id)})

    assert allows(FeedbackDeletePermission, request)


def test_feedback_left_by_someone_else_is_not_deletable_by_a_member(
    session, organization, grant_role, as_request
):
    """The owner check must compare emails, not merely find a contact."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    someone_else = IndividualContactFactory()
    session.commit()

    request = as_request(
        user, organization, path_params={"individual_contact_id": str(someone_else.id)}
    )

    assert not allows(FeedbackDeletePermission, request)


# --- Scribes and self-service contact edits ------------------------------


def test_a_scribe_may_edit_incident_events_and_a_plain_participant_may_not(
    session, organization, grant_role, as_request
):
    """IncidentEventPermission is what lets the timeline be rewritten.

    The scribe role exists precisely to grant that without making someone an
    organization admin, so the two participants below differ only in role.
    """
    scribe_user = DispatchUserFactory()
    plain_user = DispatchUserFactory()
    grant_role(scribe_user, organization, UserRoles.member)
    grant_role(plain_user, organization, UserRoles.member)

    incident = IncidentFactory(visibility=Visibility.open)
    incident.participants.append(
        ParticipantFactory(
            individual=IndividualContactFactory(email=scribe_user.email),
            participant_roles=[ParticipantRoleFactory(role=ParticipantRoleType.scribe)],
        )
    )
    incident.participants.append(
        ParticipantFactory(
            individual=IndividualContactFactory(email=plain_user.email),
            participant_roles=[ParticipantRoleFactory(role=ParticipantRoleType.participant)],
        )
    )
    session.commit()

    params = {"incident_id": incident.id}
    assert allows(IncidentEventPermission, as_request(scribe_user, organization, params))
    assert not allows(IncidentEventPermission, as_request(plain_user, organization, params))


def test_a_user_may_edit_their_own_contact_but_not_someone_elses(
    session, organization, grant_role, as_request
):
    """Self-service profile editing, without granting it over other people.

    The match is on email, so a contact belonging to somebody else must be
    refused even though the caller has the same organization role.
    """
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)
    own_contact = IndividualContactFactory(email=user.email)
    other_contact = IndividualContactFactory()
    session.commit()

    def may_edit(contact):
        return allows(
            IndividualContactUpdatePermission,
            as_request(user, organization, path_params={"individual_contact_id": contact.id}),
        )

    assert may_edit(own_contact)
    assert not may_edit(other_contact)


def test_an_organization_admin_may_edit_any_contact(session, organization, grant_role, as_request):
    """The admin override, so the email match above is a grant not a wall."""
    admin = DispatchUserFactory()
    grant_role(admin, organization, UserRoles.admin)
    someone_else = IndividualContactFactory()
    session.commit()

    request = as_request(
        admin, organization, path_params={"individual_contact_id": someone_else.id}
    )

    assert allows(IndividualContactUpdatePermission, request)
