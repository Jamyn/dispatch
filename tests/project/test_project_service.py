from sqlalchemy import func


def test_get(session, project):
    from dispatch.project.service import get

    t_project = get(db_session=session, project_id=project.id)
    assert t_project.id == project.id


def test_create(session, organization):
    from dispatch.project.service import create
    from dispatch.project.models import ProjectCreate
    from dispatch.organization.models import OrganizationRead
    import random

    name = "name"
    description = "description"
    default = True
    color = "red"

    # Convert organization to OrganizationRead
    org_read = OrganizationRead(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        description=organization.description,
    )

    # Generate a random integer ID for the project to avoid collisions
    # Use a high range to avoid conflicts with existing IDs
    project_id = random.randint(100000, 999999)

    project_in = ProjectCreate(
        id=project_id,
        name=name,
        description=description,
        default=default,
        color=color,
        organization=org_read,
        annual_employee_cost=50000,
        business_year_hours=2080,
        display_name="",
        owner_email=None,
        owner_conversation=None,
        send_daily_reports=True,
        send_weekly_reports=False,
        weekly_report_notification_id=None,
        enabled=True,
        storage_folder_one=None,
        storage_folder_two=None,
        storage_use_folder_one_as_primary=True,
        storage_use_title=False,
        allow_self_join=True,
        select_commander_visibility=True,
        report_incident_instructions=None,
        report_incident_title_hint=None,
        report_incident_description_hint=None,
        snooze_extension_oncall_service=None,
    )
    project = create(db_session=session, project_in=project_in)
    assert project


def test_update(session, project):
    from dispatch.project.service import update
    from dispatch.project.models import ProjectUpdate

    name = "Updated name"

    project_in = ProjectUpdate(
        id=project.id,
        name=name,
        annual_employee_cost=50000,
        business_year_hours=2080,
        snooze_extension_oncall_service=None,
        stable_priority_id=None,
        snooze_extension_oncall_service_id=None,
    )
    project = update(
        db_session=session,
        project=project,
        project_in=project_in,
    )
    assert project.name == name


def test_delete(session, project):
    from dispatch.project.service import delete, get

    delete(db_session=session, project_id=project.id)
    assert not get(db_session=session, project_id=project.id)


def test_get_by_name_or_default__name(session, project):
    from dispatch.project.models import ProjectRead
    from dispatch.project.service import get_by_name_or_default

    project_in = ProjectRead.from_orm(project)
    result = get_by_name_or_default(db_session=session, project_in=project_in)
    assert result.id == project.id


def test_get_by_name_or_default__default(session, project, organization):
    from dispatch.project.models import ProjectRead
    from dispatch.project.service import get_by_name_or_default

    # Ensure only one default project
    for p in session.query(type(project)).all():
        p.default = False
    project.default = True
    session.commit()
    # Pass a ProjectRead with a non-existent name
    project_in = ProjectRead(name="nonexistent", organization=organization)
    result = get_by_name_or_default(db_session=session, project_in=project_in)
    assert result.id == project.id


def test_get_all_enabled_limits_in_the_database(session):
    """The Slack project type-ahead runs on every keystroke against a table
    that can hold thousands of rows, so the limit has to be the database's."""
    from sqlalchemy import event

    from dispatch.database.core import engine
    from dispatch.project.service import get_all_enabled
    from tests.factories import ProjectFactory

    ProjectFactory.create_batch(3, enabled=True)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        results = get_all_enabled(db_session=session, query_str="proj", limit=2)
    finally:
        event.remove(engine, "after_cursor_execute", record)

    # Transaction control is the suite's per-test isolation, not work this
    # query did; the claim is that one statement answers the type-ahead.
    control = ("SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT", "BEGIN")
    queries = [s for s in statements if not s.strip().upper().startswith(control)]

    assert len(results) <= 2
    assert len(queries) == 1, statements
    assert "LIMIT" in queries[0].upper()
    assert "ORDER BY" in queries[0].upper()
    assert "ILIKE" in queries[0].upper()


def test_the_label_index_matches_the_ordering_it_exists_for(session):
    """The index expression and `get_all_enabled`'s ordering must stay identical.

    An expression index is only usable by a query that repeats the expression
    verbatim, so these two are one change split across two files -- the model's
    `__table_args__` and `project_service`'s `order_by`. Nothing links them, and
    a near miss is invisible: the query keeps working and silently goes back to
    scanning and sorting the whole table.

    Asserting on the compiled SQL rather than on a plan keeps this deterministic
    -- the planner will not choose an index on a table holding a handful of test
    rows, so a plan assertion here would prove nothing.
    """
    import re

    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql

    from dispatch.project.models import Project
    from dispatch.project.service import PROJECT_LABEL, _relevance

    def canonical(expression: str) -> str:
        """Strip everything the two renderings disagree on but the shape.

        Postgres reports the index with bare, parenthesised column names; the
        compiled ORDER BY carries the tenant schema qualifier and no parens.
        Reducing both to their operators and identifiers compares what the
        planner actually cares about -- the expression -- without pinning either
        side's punctuation.
        """
        expression = expression.lower().replace("::text", "")
        expression = re.sub(r"\b[a-z_][a-z0-9_]*\.", "", expression)  # schema/table qualifiers
        return re.sub(r"[\s(),]", "", expression)

    # The ordering the no-query probe issues -- `project_select` builds every
    # modal that offers a project this way.
    assert not _relevance(None), "a no-query call must not lead with a relevance term"
    ordering = [*_relevance(None), func.lower(PROJECT_LABEL), Project.id]

    order_sql = " ".join(
        str(term.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for term in ordering
    )

    indexdef = session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'project' AND indexname = 'project_label_idx'"
        )
    ).scalar_one_or_none()

    assert indexdef, "project_label_idx is missing; the model no longer declares it"

    index_sql = indexdef.lower().split("using btree (", 1)[1].rsplit(")", 1)[0]

    assert canonical(index_sql) == canonical(order_sql), (
        "the index expression and the query's ordering have drifted, so the index "
        f"can no longer serve it:\n  index: {index_sql}\n  order: {order_sql}"
    )
