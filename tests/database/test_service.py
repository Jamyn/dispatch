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


def page_through_every_row(session, current_user, model, items_per_page, **kwargs):
    """Collects every id the caller would see paging from the first page to the last."""
    seen = []
    page = 1
    while True:
        result = search_filter_sort_paginate(
            db_session=session,
            model=model,
            page=page,
            items_per_page=items_per_page,
            current_user=current_user,
            role=UserRoles.admin,
            **kwargs,
        )
        if not result["items"]:
            return seen
        seen.extend(item.id for item in result["items"])
        page += 1
        # a pager that stops advancing would otherwise hang the suite
        assert page < 100, "paging never reached the end"


def test_paging_a_model_needing_distinct_shows_every_row_exactly_once(session, admin_user, project):
    """Given enough rows, when paging a DISTINCT model, then no row repeats or vanishes.

    Tag goes down the models_needing_distinct branch, which sends no sort of
    its own here. Postgres is then free to hand LIMIT/OFFSET a different row
    order per page -- with a hash aggregate it does, and rows land on two pages
    while others are never returned at all.
    """
    from tests.factories import TagFactory, TagTypeFactory

    tag_type = TagTypeFactory(project=project)
    tags = [TagFactory(project=project, tag_type=tag_type) for _ in range(200)]
    session.commit()

    seen = page_through_every_row(session, admin_user, "Tag", items_per_page=20)

    assert len(seen) == len(set(seen)), "a row came back on more than one page"
    assert set(seen) == {tag.id for tag in tags}, "paging never returned every row"


def paginated_order_by(session, current_user, **kwargs):
    """The trailing ORDER BY of the statement that carries LIMIT/OFFSET.

    Relationship loaders fire their own offset queries afterwards, so only the
    first one is the paginated query itself.
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        search_filter_sort_paginate(
            db_session=session,
            page=1,
            items_per_page=5,
            current_user=current_user,
            role=UserRoles.admin,
            **kwargs,
        )
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    paginated = next(s for s in statements if "OFFSET" in s)
    # the outermost ORDER BY, with any LIMIT/OFFSET that trails it removed
    _, found, order_by = paginated.rpartition("ORDER BY")
    return order_by.partition("LIMIT")[0].strip() if found else ""


def test_every_paginated_query_ends_with_the_primary_key(session, admin_user, project):
    """Given any query shape, when it is paginated, then ORDER BY ends with the key.

    Only a unique trailing key makes LIMIT/OFFSET a partition of the result
    set. Asserted on the SQL because the reorder it prevents depends on the
    plan Postgres happens to pick, which a test cannot force for every shape.
    """
    from tests.factories import IncidentFactory, TagFactory, TagTypeFactory

    tag = TagFactory(project=project, tag_type=TagTypeFactory(project=project))
    IncidentFactory(project=project, tags=[tag])
    session.commit()

    # the key is aliased differently per shape, so match the table it belongs to
    shapes = {
        "no sort at all": ("incident", {"model": "Incident"}),
        "a sort the client sent": (
            "incident",
            {"model": "Incident", "sort_by": ["title"], "descending": [False]},
        ),
        "the intersected tag_all filter": (
            "incident",
            {"model": "Incident", "filter_spec": json.dumps(tag_all_spec(tag))},
        ),
        "the models_needing_distinct branch": ("tag", {"model": "Tag"}),
        "a client sort on the models_needing_distinct branch": (
            "tag",
            {"model": "Tag", "sort_by": ["name"], "descending": [False]},
        ),
        "a dropped sort on the models_needing_distinct branch": (
            "tag",
            {"model": "Tag", "sort_by": ["tag_type.name"], "descending": [False]},
        ),
    }

    for shape, (table, kwargs) in shapes.items():
        order_by = paginated_order_by(session, admin_user, **kwargs)
        assert order_by, f"{shape}: paginated without any ORDER BY"
        last_key = order_by.split(",")[-1].strip()
        assert last_key.endswith((f"{table}.id", f"{table}_id")), (
            f"{shape}: ORDER BY does not end with {table}'s primary key -- {order_by}"
        )


def tags_in_project(project):
    """Restricts a Tag search to one project, so unrelated fixture rows cannot reorder it."""
    return json.dumps(
        {"and": [{"or": [{"model": "Project", "field": "id", "op": "==", "value": project.id}]}]}
    )


@pytest.mark.parametrize("descending", [False, True])
def test_a_distinct_model_honours_a_sort_on_its_own_column(
    session, admin_user, project, descending
):
    """Given a sort on the model's own column, when the DISTINCT branch runs,
    then the rows come back in that order.

    Tag is the only model on that branch, and it dropped every ORDER BY before
    paginating -- so clicking a sortable column header on the Tags table did
    nothing.
    """
    from tests.factories import TagFactory, TagTypeFactory

    tag_type = TagTypeFactory(project=project)
    for name in ("zulu", "alpha", "mike"):
        TagFactory(name=name, project=project, tag_type=tag_type)

    result = search_filter_sort_paginate(
        db_session=session,
        model="Tag",
        filter_spec=tags_in_project(project),
        sort_by=["name"],
        descending=[descending],
        current_user=admin_user,
        role=UserRoles.admin,
        items_per_page=50,
    )

    expected = ["zulu", "mike", "alpha"] if descending else ["alpha", "mike", "zulu"]
    assert [tag.name for tag in result["items"]] == expected


def test_a_distinct_model_sorted_by_a_joined_column_still_returns_its_rows(
    session, admin_user, project
):
    """Given a sort on a joined column, when the DISTINCT branch runs, then the
    rows still come back.

    A joined table's column is not in the SELECT DISTINCT list, so keeping it
    makes Postgres reject the statement -- and this branch runs it outside the
    ProgrammingError handler, so the request 500s rather than degrading.
    """
    from tests.factories import TagFactory, TagTypeFactory

    for type_name in ("zeta", "beta"):
        TagFactory(project=project, tag_type=TagTypeFactory(name=type_name, project=project))

    result = search_filter_sort_paginate(
        db_session=session,
        model="Tag",
        filter_spec=tags_in_project(project),
        sort_by=["tag_type.name"],
        descending=[False],
        current_user=admin_user,
        role=UserRoles.admin,
        items_per_page=50,
    )

    assert result["total"] == 2, "sorting by a joined column returned no page"


def test_a_distinct_model_searched_without_a_sort_still_returns_its_matches(
    session, admin_user, project
):
    """Given a free-text search and no sort, when the DISTINCT branch runs, then
    the matches come back.

    With no sort_by, `search` orders by ts_rank_cd -- an expression rather than
    a column of the select list, so it fails the same way a joined column does.
    """
    from tests.factories import TagFactory, TagTypeFactory

    marker = f"needle{uuid.uuid4().hex[:8]}"
    TagFactory(name=marker, project=project, tag_type=TagTypeFactory(project=project))

    result = search_filter_sort_paginate(
        db_session=session,
        model="Tag",
        query_str=marker,
        current_user=admin_user,
        role=UserRoles.admin,
        items_per_page=50,
    )

    assert [tag.name for tag in result["items"]] == [marker]
    assert result["total"] == 1


def tags_of_type_in_project(project, type_name):
    """The filter the tag autocomplete sends for a `type/name` query."""
    return json.dumps(
        {
            "and": [
                {"or": [{"model": "Project", "field": "id", "op": "==", "value": project.id}]},
                {"or": [{"model": "TagType", "field": "name", "op": "==", "value": type_name}]},
            ]
        }
    )


def test_a_tag_type_clause_narrows_a_tag_search_to_that_type(session, admin_user, project):
    """Given a TagType clause on a Tag search, when it runs, then only that
    type's tags come back.

    tag_type carries a project_id of its own, so auto-join reached it through
    project rather than through tag.tag_type_id -- leaving every tag in the
    project eligible whenever the named type existed at all (#260).
    """
    from tests.factories import TagFactory, TagTypeFactory

    wanted_type = TagTypeFactory(name="incident-type", project=project)
    other_type = TagTypeFactory(name="service", project=project)
    wanted = TagFactory(name="alpha-one", project=project, tag_type=wanted_type)
    TagFactory(name="alpha-two", project=project, tag_type=other_type)

    result = search_filter_sort_paginate(
        db_session=session,
        model="Tag",
        query_str="alpha",
        filter_spec=tags_of_type_in_project(project, "incident-type"),
        sort_by=["tag_type.name"],
        descending=[False],
        current_user=admin_user,
        role=UserRoles.admin,
        items_per_page=50,
    )

    assert [tag.id for tag in result["items"]] == [wanted.id]
    assert result["total"] == 1


def test_a_tag_type_id_clause_still_returns_each_matching_tag_once(session, admin_user, project):
    """Given the id-form clause the tag store sends, then each tag comes back once.

    `TagType.id` is rewritten to tag.tag_type_id before it reaches SQL, so this
    clause was already correct; it shares the join the type-name clause now
    uses, and a join that multiplies rows would inflate the total here.
    """
    from tests.factories import TagFactory, TagTypeFactory

    wanted_type = TagTypeFactory(project=project)
    TagTypeFactory(project=project)
    wanted = [TagFactory(project=project, tag_type=wanted_type) for _ in range(3)]
    TagFactory(project=project, tag_type=TagTypeFactory(project=project))

    filter_spec = json.dumps(
        {
            "and": [
                {"or": [{"model": "Project", "field": "id", "op": "==", "value": project.id}]},
                {"or": [{"model": "TagType", "field": "id", "op": "==", "value": wanted_type.id}]},
            ]
        }
    )

    result = search_filter_sort_paginate(
        db_session=session,
        model="Tag",
        filter_spec=filter_spec,
        current_user=admin_user,
        role=UserRoles.admin,
        items_per_page=50,
    )

    assert sorted(tag.id for tag in result["items"]) == sorted(tag.id for tag in wanted)
    assert result["total"] == len(wanted)
