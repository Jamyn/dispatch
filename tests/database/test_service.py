import pytest
import json
import uuid
from json.decoder import JSONDecodeError
from sqlalchemy_filters.exceptions import BadFilterFormat
from dispatch.database.service import (
    Operator,
    Filter,
    search_filter_sort_paginate,
    restricted_incident_filter,
    apply_filters,
)
from dispatch.database.service import restricted_case_filter
from dispatch.enums import UserRoles, Visibility
from dispatch.incident.models import Incident
from dispatch.individual.models import IndividualContact
from dispatch.participant.models import Participant
from dispatch.project.models import Project
from dispatch.case.models import Case


def test_operator_invalid():
    """Tests that invalid operators raise BadFilterFormat."""
    with pytest.raises(BadFilterFormat):
        Operator("invalid_operator")


def test_filter_missing_field():
    """Tests that missing field raises BadFilterFormat."""
    with pytest.raises(BadFilterFormat):
        Filter({})


def test_filter_invalid_spec():
    """Tests that invalid filter spec raises BadFilterFormat."""
    with pytest.raises(BadFilterFormat):
        Filter(None)


# Test search_filter_sort_paginate
def test_search_filter_sort_paginate_basic(session, user):
    """Tests basic functionality of search_filter_sort_paginate."""
    result = search_filter_sort_paginate(
        db_session=session, model="Incident", current_user=user, role=UserRoles.member
    )

    assert isinstance(result, dict)
    assert "items" in result
    assert "itemsPerPage" in result
    assert "page" in result
    assert "total" in result


def test_basic_pagination(session, incidents, admin_user):
    """Test basic pagination functionality."""
    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        page=1,
        items_per_page=2,
        current_user=admin_user,
        role=UserRoles.admin,
    )

    assert result["page"] == 1
    assert result["itemsPerPage"] == 2
    assert len(result["items"]) == 2


def test_simple_filter_specification(session, incidents, admin_user):
    """Test filtering with simple filter specification."""
    filter_spec = {"field": "visibility", "op": "==", "value": "open"}

    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        filter_spec=json.dumps(filter_spec),
        current_user=admin_user,
        role=UserRoles.admin,
    )

    assert all(incident.visibility == Visibility.open for incident in result["items"])


def test_sorting_functionality(session, incidents, user):
    """Test sorting functionality."""
    # Create a unique prefix for our test incidents to ensure isolation

    test_prefix = f"SORT_TEST_{uuid.uuid4().hex[:8]}_"

    # Ensure clean session state
    session.expire_all()

    # Create test incidents with predictable titles for sorting

    test_incidents = []
    test_titles = [f"{test_prefix}Alpha", f"{test_prefix}Beta", f"{test_prefix}Charlie"]

    for title in test_titles:
        incident = Incident(
            title=title,
            description="Test incident for sorting",
            visibility=Visibility.open,
            project_id=incidents[0].project_id,  # Use same project as fixture incidents
        )
        session.add(incident)
        test_incidents.append(incident)

    session.flush()

    try:
        # Use filter instead of search to find our test incidents
        filter_spec = {"field": "title", "op": "like", "value": f"{test_prefix}%"}

        result = search_filter_sort_paginate(
            db_session=session,
            model="Incident",
            filter_spec=json.dumps(filter_spec),  # Filter to only our test incidents
            sort_by=["title"],
            descending=[True],
            current_user=user,
        )

        titles = [incident.title for incident in result["items"]]
        expected_titles = sorted(test_titles, reverse=True)

        assert titles == expected_titles, f"Expected {expected_titles}, got {titles}"

    finally:
        # Clean up our test incidents
        for incident in test_incidents:
            session.delete(incident)
        session.flush()


def test_unlimited_pagination(session, incidents, admin_user):
    """Test pagination with unlimited items per page."""
    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        items_per_page=-1,
        current_user=admin_user,
        role=UserRoles.admin,
    )

    assert len(result["items"]) == result["total"]  # All items


def test_empty_query_string(session, incidents, admin_user):
    """Test behavior with empty query string."""
    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        query_str="",
        current_user=admin_user,
        role=UserRoles.admin,
    )

    assert len(result["items"]) > 0  # Should return all items


def test_invalid_filter_spec(session, incidents, user):
    """Test behavior with invalid filter specification."""
    with pytest.raises(JSONDecodeError):  # Adjust exception type as needed
        search_filter_sort_paginate(
            db_session=session,
            model="Incident",
            filter_spec="invalid_json",
            current_user=user,
        )


def test_pagination_out_of_bounds(session, incidents, user):
    """Test pagination when page number is out of bounds."""
    result = search_filter_sort_paginate(
        db_session=session, model="Incident", page=999, items_per_page=5, current_user=user
    )

    assert len(result["items"]) == 0
    assert result["page"] == 999


def test_role_based_filtering(session, incidents, user, admin_user):
    """Test filtering based on user role."""
    # Test admin access
    admin_result = search_filter_sort_paginate(
        db_session=session, model="Incident", current_user=admin_user, role=UserRoles.admin
    )

    # Test member access
    member_result = search_filter_sort_paginate(
        db_session=session, model="Incident", current_user=user, role=UserRoles.member
    )

    assert len(admin_result["items"]) >= len(member_result["items"])


# Test restricted filters
def test_restricted_incident_filter_with_test_data(session, user, admin_user):
    """Test incident filtering with comprehensive test data setup."""
    # Store original user email to restore later
    original_user_email = getattr(user, "email", "test@example.com")

    # Create unique identifiers to avoid conflicts
    timestamp = int(__import__("time").time() * 1000000)  # microsecond precision
    test_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"

    # Create unique test data
    project_name = f"test_project_{test_id}"
    user_email = f"test_user_{test_id}@example.com"
    other_email = f"other_user_{test_id}@example.com"

    project = None  # Initialize to avoid unbound variable error

    # Ensure clean session state
    session.expire_all()

    try:
        # Create test project
        project = Project(name=project_name, default=False)
        session.add(project)
        session.flush()  # Use flush instead of commit to get ID within transaction

        # Create test individual contacts
        regular_user_contact = IndividualContact(
            email=user_email, name="Regular User", project_id=project.id
        )
        other_user_contact = IndividualContact(
            email=other_email, name="Other User", project_id=project.id
        )
        session.add_all([regular_user_contact, other_user_contact])
        session.flush()

        # Create test incidents with unique titles
        test_prefix = f"TEST_{test_id}"
        incidents_to_create = [
            Incident(
                title=f"{test_prefix}_Open_Incident_1",
                description="Description",
                visibility=Visibility.open,
                project_id=project.id,
            ),
            Incident(
                title=f"{test_prefix}_Open_Incident_2",
                description="Description",
                visibility=Visibility.open,
                project_id=project.id,
            ),
            Incident(
                title=f"{test_prefix}_Restricted_User_Participant",
                description="Description",
                visibility=Visibility.restricted,
                project_id=project.id,
            ),
            Incident(
                title=f"{test_prefix}_Restricted_No_Participant",
                description="Description",
                visibility=Visibility.restricted,
                project_id=project.id,
            ),
        ]

        session.add_all(incidents_to_create)
        session.flush()

        # Get the created incidents
        created_incidents = (
            session.query(Incident).filter(Incident.title.like(f"{test_prefix}%")).all()
        )

        restricted_user_participant = next(
            i for i in created_incidents if "User_Participant" in i.title
        )

        # Create participants - user is participant in one restricted incident
        user_participant = Participant(
            incident_id=restricted_user_participant.id,
            individual_contact_id=regular_user_contact.id,
        )
        session.add(user_participant)
        session.flush()

        # Temporarily change user email for testing
        user.email = user_email

        # Test admin role - should see all incidents (filter by project to avoid other test data)
        admin_query = session.query(Incident).filter(Incident.project_id == project.id)
        admin_filtered = restricted_incident_filter(admin_query, admin_user, UserRoles.admin)
        admin_results = admin_filtered.all()
        assert len(admin_results) == 4, (
            f"Admin should see all 4 incidents, got {len(admin_results)}"
        )

        # Test owner role - should see all incidents
        owner_query = session.query(Incident).filter(Incident.project_id == project.id)
        owner_filtered = restricted_incident_filter(owner_query, user, UserRoles.owner)
        owner_results = owner_filtered.all()
        assert len(owner_results) == 4, (
            f"Owner should see all 4 incidents, got {len(owner_results)}"
        )

        # Test manager role - should see all incidents
        manager_query = session.query(Incident).filter(Incident.project_id == project.id)
        manager_filtered = restricted_incident_filter(manager_query, user, UserRoles.manager)
        manager_results = manager_filtered.all()
        assert len(manager_results) == 4, (
            f"Manager should see all 4 incidents, got {len(manager_results)}"
        )

        # Test member role - should see open incidents + restricted where user is participant
        member_query = session.query(Incident).filter(Incident.project_id == project.id)
        member_filtered = restricted_incident_filter(member_query, user, UserRoles.member)
        member_results = member_filtered.all()
        assert len(member_results) == 3, (
            f"Member should see 3 incidents (2 open + 1 restricted as participant), got {len(member_results)}"
        )

        # Verify member sees correct incidents
        member_titles = {incident.title for incident in member_results}
        expected_titles = {
            f"{test_prefix}_Open_Incident_1",
            f"{test_prefix}_Open_Incident_2",
            f"{test_prefix}_Restricted_User_Participant",
        }
        assert member_titles == expected_titles, (
            f"Member should see {expected_titles}, got {member_titles}"
        )

    finally:
        # Always restore original user email
        user.email = original_user_email

        # Clean up test data in reverse order
        try:
            if project and hasattr(project, "id"):
                # Clean up participants
                session.query(Participant).filter(
                    Participant.incident_id.in_(
                        session.query(Incident.id).filter(Incident.project_id == project.id)
                    )
                ).delete(synchronize_session=False)

                # Clean up incidents
                session.query(Incident).filter(Incident.project_id == project.id).delete(
                    synchronize_session=False
                )

                # Clean up contacts
                session.query(IndividualContact).filter(
                    IndividualContact.project_id == project.id
                ).delete(synchronize_session=False)

                # Clean up project
                session.query(Project).filter(Project.id == project.id).delete(
                    synchronize_session=False
                )

                session.flush()
        except Exception as cleanup_error:
            # If cleanup fails, at least try to rollback
            session.rollback()
            pytest.warns(UserWarning, f"Cleanup failed: {cleanup_error}")


def test_restricted_case_filter_with_test_data(session, user, admin_user):
    """Test case filtering with comprehensive test data setup."""
    # Store original user email to restore later
    original_user_email = getattr(user, "email", "test@example.com")

    # Create unique identifiers to avoid conflicts
    timestamp = int(__import__("time").time() * 1000000)  # microsecond precision
    test_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"

    # Create unique test data
    project_name = f"test_case_project_{test_id}"
    user_email = f"test_case_user_{test_id}@example.com"
    other_email = f"other_case_user_{test_id}@example.com"

    project = None  # Initialize to avoid unbound variable error

    # Ensure clean session state
    session.expire_all()

    try:
        # Create test project
        project = Project(name=project_name, default=False)
        session.add(project)
        session.flush()  # Use flush instead of commit to get ID within transaction

        # Create test individual contacts
        regular_user_contact = IndividualContact(
            email=user_email, name="Regular User", project_id=project.id
        )
        other_user_contact = IndividualContact(
            email=other_email, name="Other User", project_id=project.id
        )
        session.add_all([regular_user_contact, other_user_contact])
        session.flush()

        # Create test cases with unique titles
        test_prefix = f"CASE_TEST_{test_id}"
        cases_to_create = [
            Case(
                title=f"{test_prefix}_Open_Case_1",
                description="Description",
                visibility=Visibility.open,
                project_id=project.id,
            ),
            Case(
                title=f"{test_prefix}_Open_Case_2",
                description="Description",
                visibility=Visibility.open,
                project_id=project.id,
            ),
            Case(
                title=f"{test_prefix}_Restricted_User_Participant",
                description="Description",
                visibility=Visibility.restricted,
                project_id=project.id,
            ),
            Case(
                title=f"{test_prefix}_Restricted_No_Participant",
                description="Description",
                visibility=Visibility.restricted,
                project_id=project.id,
            ),
        ]

        session.add_all(cases_to_create)
        session.flush()

        # Get the created cases
        created_cases = session.query(Case).filter(Case.title.like(f"{test_prefix}%")).all()

        restricted_user_participant = next(
            c for c in created_cases if "User_Participant" in c.title
        )

        # Create participants - user is participant in one restricted case
        user_participant = Participant(
            case_id=restricted_user_participant.id, individual_contact_id=regular_user_contact.id
        )
        session.add(user_participant)
        session.flush()

        # Temporarily change user email for testing
        user.email = user_email

        # Test admin role - should see all cases
        admin_query = session.query(Case).filter(Case.project_id == project.id)
        admin_filtered = restricted_case_filter(admin_query, admin_user, UserRoles.admin)
        admin_results = admin_filtered.all()
        assert len(admin_results) == 4, f"Admin should see all 4 cases, got {len(admin_results)}"

        # Test owner role - should see all cases
        owner_query = session.query(Case).filter(Case.project_id == project.id)
        owner_filtered = restricted_case_filter(owner_query, user, UserRoles.owner)
        owner_results = owner_filtered.all()
        assert len(owner_results) == 4, f"Owner should see all 4 cases, got {len(owner_results)}"

        # Test manager role - should see all cases
        manager_query = session.query(Case).filter(Case.project_id == project.id)
        manager_filtered = restricted_case_filter(manager_query, user, UserRoles.manager)
        manager_results = manager_filtered.all()
        assert len(manager_results) == 4, (
            f"Manager should see all 4 cases, got {len(manager_results)}"
        )

        # Test member role - should see open cases + restricted where user is participant
        member_query = session.query(Case).filter(Case.project_id == project.id)
        member_filtered = restricted_case_filter(member_query, user, UserRoles.member)
        member_results = member_filtered.all()
        assert len(member_results) == 3, (
            f"Member should see 3 cases (2 open + 1 restricted as participant), got {len(member_results)}"
        )

        # Verify member sees correct cases
        member_titles = {case.title for case in member_results}
        expected_titles = {
            f"{test_prefix}_Open_Case_1",
            f"{test_prefix}_Open_Case_2",
            f"{test_prefix}_Restricted_User_Participant",
        }
        assert member_titles == expected_titles, (
            f"Member should see {expected_titles}, got {member_titles}"
        )

    finally:
        # Always restore original user email
        user.email = original_user_email

        # Clean up test data in reverse order
        try:
            if project and hasattr(project, "id"):
                # Clean up participants
                session.query(Participant).filter(
                    Participant.case_id.in_(
                        session.query(Case.id).filter(Case.project_id == project.id)
                    )
                ).delete(synchronize_session=False)

                # Clean up cases
                session.query(Case).filter(Case.project_id == project.id).delete(
                    synchronize_session=False
                )

                # Clean up contacts
                session.query(IndividualContact).filter(
                    IndividualContact.project_id == project.id
                ).delete(synchronize_session=False)

                # Clean up project
                session.query(Project).filter(Project.id == project.id).delete(
                    synchronize_session=False
                )

                session.flush()
        except Exception as cleanup_error:
            # If cleanup fails, at least try to rollback
            session.rollback()
            print(f"Warning: Cleanup failed: {cleanup_error}")


def test_participant_based_filtering_edge_cases(session, user):
    """Test edge cases in participant-based filtering logic."""
    # Store original user email to restore later
    original_user_email = getattr(user, "email", "test@example.com")

    # Create unique identifiers to avoid conflicts
    timestamp = int(__import__("time").time() * 1000000)  # microsecond precision
    test_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"

    # Create unique test data
    project_name = f"test_edge_project_{test_id}"
    user_email = f"edge_test_user_{test_id}@example.com"
    other_email = f"edge_other_user_{test_id}@example.com"

    project = None  # Initialize to avoid unbound variable error

    # Ensure clean session state
    session.expire_all()

    try:
        # Create test project
        project = Project(name=project_name, default=False)
        session.add(project)
        session.flush()  # Use flush instead of commit to get ID within transaction

        # Create test individual contacts
        user_contact = IndividualContact(email=user_email, name="Test User", project_id=project.id)
        other_contact = IndividualContact(
            email=other_email, name="Other User", project_id=project.id
        )
        session.add_all([user_contact, other_contact])
        session.flush()

        # Test case: Multiple participants, user is one of them
        test_prefix = f"EDGE_TEST_{test_id}"
        restricted_incident = Incident(
            title=f"{test_prefix}_Multi_Participant_Restricted_Incident",
            description="Description",
            visibility=Visibility.restricted,
            project_id=project.id,
        )
        session.add(restricted_incident)
        session.flush()

        # Create multiple participants including our user
        user_participant = Participant(
            incident_id=restricted_incident.id, individual_contact_id=user_contact.id
        )
        other_participant = Participant(
            incident_id=restricted_incident.id, individual_contact_id=other_contact.id
        )
        session.add_all([user_participant, other_participant])
        session.flush()

        # Temporarily change user email for testing
        user.email = user_email

        # Test that user can see restricted incident even with multiple participants
        query = session.query(Incident).filter(Incident.project_id == project.id)
        filtered_query = restricted_incident_filter(query, user, UserRoles.member)
        results = filtered_query.all()

        assert len(results) == 1, f"Should see 1 incident, got {len(results)}"
        assert results[0].title == f"{test_prefix}_Multi_Participant_Restricted_Incident"

        # Test user not in participants cannot see restricted incident
        non_participant_email = f"nonparticipant_{test_id}@example.com"
        non_participant_user = user.__class__(
            email=non_participant_email
        )  # Properly initialize user
        query = session.query(Incident).filter(Incident.project_id == project.id)
        filtered_query = restricted_incident_filter(query, non_participant_user, UserRoles.member)
        results = filtered_query.all()

        assert len(results) == 0, f"Non-participant should see 0 incidents, got {len(results)}"

    finally:
        # Always restore original user email
        user.email = original_user_email

        # Clean up test data in reverse order
        try:
            if project and hasattr(project, "id"):
                # Clean up participants
                session.query(Participant).filter(
                    Participant.incident_id.in_(
                        session.query(Incident.id).filter(Incident.project_id == project.id)
                    )
                ).delete(synchronize_session=False)

                # Clean up incidents
                session.query(Incident).filter(Incident.project_id == project.id).delete(
                    synchronize_session=False
                )

                # Clean up contacts
                session.query(IndividualContact).filter(
                    IndividualContact.project_id == project.id
                ).delete(synchronize_session=False)

                # Clean up project
                session.query(Project).filter(Project.id == project.id).delete(
                    synchronize_session=False
                )

                session.flush()
        except Exception as cleanup_error:
            # If cleanup fails, at least try to rollback
            session.rollback()
            print(f"Warning: Cleanup failed: {cleanup_error}")


# Simplified tests for basic role checks (keeping for backwards compatibility)
def test_restricted_incident_filter_member(session, user):
    """Tests incident filtering for member role."""
    query = session.query(Incident)
    filtered_query = restricted_incident_filter(
        query=query, current_user=user, role=UserRoles.member
    )

    assert filtered_query is not None


def test_restricted_incident_filter_admin(session, user):
    """Tests incident filtering for admin role."""
    query = session.query(Incident)
    filtered_query = restricted_incident_filter(
        query=query, current_user=user, role=UserRoles.admin
    )

    assert filtered_query is not None


def test_restricted_incident_filter_owner(session, user):
    """Tests incident filtering for owner role - should have unrestricted access."""
    query = session.query(Incident)
    filtered_query = restricted_incident_filter(
        query=query, current_user=user, role=UserRoles.owner
    )

    assert filtered_query is not None


def test_restricted_incident_filter_manager(session, user):
    """Tests incident filtering for manager role - should have unrestricted access."""
    query = session.query(Incident)
    filtered_query = restricted_incident_filter(
        query=query, current_user=user, role=UserRoles.manager
    )

    assert filtered_query is not None


def test_restricted_case_filter_member(session, user):
    """Tests case filtering for member role."""
    query = session.query(Case)
    filtered_query = restricted_case_filter(query=query, current_user=user, role=UserRoles.member)

    assert filtered_query is not None


def test_restricted_case_filter_admin(session, user):
    """Tests case filtering for admin role."""
    query = session.query(Case)
    filtered_query = restricted_case_filter(query=query, current_user=user, role=UserRoles.admin)

    assert filtered_query is not None


def test_restricted_case_filter_owner(session, user):
    """Tests case filtering for owner role - should have unrestricted access."""
    query = session.query(Case)
    filtered_query = restricted_case_filter(query=query, current_user=user, role=UserRoles.owner)

    assert filtered_query is not None


def test_restricted_case_filter_manager(session, user):
    """Tests case filtering for manager role - should have unrestricted access."""
    query = session.query(Case)
    filtered_query = restricted_case_filter(query=query, current_user=user, role=UserRoles.manager)

    assert filtered_query is not None


# Test apply_filters
def test_apply_filters_basic(session):
    """Tests basic filter application."""
    query = session.query(Incident)
    filter_spec = {"field": "title", "op": "==", "value": "Test"}

    filtered_query = apply_filters(query, filter_spec)
    assert filtered_query is not None


def test_apply_filters_complex(session):
    """Tests complex filter application with boolean operations."""
    query = session.query(Incident)
    filter_spec = {
        "and": [
            {"field": "title", "op": "==", "value": "Test"},
            {"field": "visibility", "op": "==", "value": "open"},
        ]
    }

    filtered_query = apply_filters(query, filter_spec)
    assert filtered_query is not None


def test_get_query_models_includes_joined_entities(session):
    """Joined models must be discoverable, or model-scoped filter specs
    (e.g. signal filters targeting Entity/EntityType) raise BadSpec."""
    from dispatch.database.service import get_query_models

    query = session.query(Incident).join(Incident.participants).join(Participant.individual)
    models = get_query_models(query)

    assert "Incident" in models
    assert "Participant" in models
    assert "IndividualContact" in models


def test_an_unknown_model_name_in_a_filter_is_reported_as_a_validation_error(session):
    """Given a filter naming no model, when resolving it, then a 422-able error is raised.

    Search filters carry user-supplied model names, so this is reachable from
    the API and has to answer 422 rather than an opaque 500.
    """
    import pytest
    from pydantic import ValidationError

    from dispatch.database.core import get_class_by_tablename

    with pytest.raises(ValidationError) as exc_info:
        get_class_by_tablename("NoSuchModel")

    assert "Model not found." in str(exc_info.value.errors())


# --- "Tag All" and "Not Case Type" -----------------------------------------
#
# Both rewrite the caller's filter spec before it becomes SQL, and neither was
# exercised. They decide which records a user is shown, so a regression that
# quietly turns "all of these tags" into "any of these tags" widens every saved
# search built on one without failing anything.


def tag_all_spec(*tags):
    """The spec the UI sends for a Tag All selection: one or-list, every tag in it."""
    return {
        "and": [
            {
                "or": [
                    {"model": "TagAll", "field": "id", "op": "==", "value": tag.id} for tag in tags
                ]
            }
        ]
    }


def test_tag_all_requires_every_tag_not_merely_one(session, admin_user, project):
    """Given two tags, when filtering on both, then only records carrying both match.

    The filter is split into one query per tag and the results intersected. If
    that ever collapses into a single or-query the filter silently becomes "any
    tag", and every saved search using it starts returning more than it should.
    """
    from tests.factories import IncidentFactory, TagFactory

    hot, cold = TagFactory(project=project), TagFactory(project=project)
    both = IncidentFactory(project=project, tags=[hot, cold])
    only_one = IncidentFactory(project=project, tags=[hot])
    session.commit()

    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        filter_spec=json.dumps(tag_all_spec(hot, cold)),
        current_user=admin_user,
        role=UserRoles.admin,
    )

    names = {incident.name for incident in result["items"]}
    assert both.name in names
    assert only_one.name not in names, "an incident with only one of the tags matched"


def test_tag_all_with_a_single_tag_still_matches(session, admin_user, project):
    """Given one tag, when filtering on it, then records carrying it match.

    The single-tag case goes down the same intersect path, so it is worth
    separating from the two-tag case.
    """
    from tests.factories import IncidentFactory, TagFactory

    tag = TagFactory(project=project)
    tagged = IncidentFactory(project=project, tags=[tag])
    untagged = IncidentFactory(project=project, tags=[])
    session.commit()

    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        filter_spec=json.dumps(tag_all_spec(tag)),
        current_user=admin_user,
        role=UserRoles.admin,
    )

    names = {incident.name for incident in result["items"]}
    assert tagged.name in names
    assert untagged.name not in names


def not_case_type_spec(case_type):
    """The spec the UI sends to exclude a case type."""
    return {
        "and": [
            {"or": [{"model": "NotCaseType", "field": "id", "op": "==", "value": case_type.id}]}
        ]
    }


def test_not_case_type_excludes_only_that_type(session, admin_user, project):
    """Given a case type to exclude, when filtering, then only other types come back.

    The spec arrives with `==` and is rewritten to `!=`. Losing that rewrite
    inverts the filter into the exact opposite of what was asked for.
    """
    from tests.factories import CaseFactory, CaseTypeFactory

    unwanted = CaseTypeFactory(project=project)
    wanted = CaseTypeFactory(project=project)
    excluded = CaseFactory(project=project, case_type=unwanted)
    kept = CaseFactory(project=project, case_type=wanted)
    session.commit()

    result = search_filter_sort_paginate(
        db_session=session,
        model="Case",
        filter_spec=json.dumps(not_case_type_spec(unwanted)),
        current_user=admin_user,
        role=UserRoles.admin,
    )

    names = {case.name for case in result["items"]}
    assert kept.name in names
    assert excluded.name not in names, "the excluded case type came back anyway"


def test_a_null_check_is_a_filter_in_its_own_right(session, admin_user, project):
    """Given `is_null`, when filtering, then only records missing that value match.

    `is_null` and `is_not_null` take no value, so they go down a separate
    single-argument path that nothing else in the filter code exercises.
    """
    from tests.factories import IncidentFactory

    from datetime import datetime

    never_stable = IncidentFactory(project=project, stable_at=None)
    went_stable = IncidentFactory(project=project, stable_at=datetime(2026, 1, 1))
    session.commit()

    def names_for(op):
        result = search_filter_sort_paginate(
            db_session=session,
            model="Incident",
            filter_spec=json.dumps({"field": "stable_at", "op": op}),
            current_user=admin_user,
            role=UserRoles.admin,
        )
        return {incident.name for incident in result["items"]}

    missing = names_for("is_null")
    assert never_stable.name in missing
    assert went_stable.name not in missing

    present = names_for("is_not_null")
    assert went_stable.name in present
    assert never_stable.name not in present


def test_an_unparseable_search_leaves_the_session_usable(session, incidents, admin_user):
    """Given a search Postgres cannot parse, when it fails, then the session still works.

    Search strings come straight from a user, and `tsq_parse` rejects stray
    tsquery operators. Postgres aborts the transaction on that error, so
    returning empty without rolling back left everything later in the request
    failing with InFailedSqlTransaction.
    """
    from dispatch.incident.models import Incident

    result = search_filter_sort_paginate(
        db_session=session,
        model="Incident",
        query_str="foo & | bar",
        current_user=admin_user,
        role=UserRoles.admin,
    )
    assert result["total"] == 0

    # the request is not over: whatever runs next must still be able to query
    assert session.query(Incident).count() >= 0, "the session was left in an aborted transaction"
