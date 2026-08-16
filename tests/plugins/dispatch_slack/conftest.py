"""Shared setup for driving the Slack modal handlers.

The handlers are written for Bolt's injecting dispatcher, so they cannot simply
be called -- their signatures differ in both argument set and order, and they
read some inputs off the Bolt context rather than taking them as arguments.
These fixtures build a plausible invocation and hand back the view the handler
tried to open.
"""

import inspect
from uuid import uuid4
from unittest.mock import MagicMock, patch

import pytest
from slack_bolt import BoltContext
from slack_bolt.request import BoltRequest
from slack_sdk.web import SlackResponse, WebClient
from sqlalchemy import insert

import dispatch.database.core as database_core
from dispatch.organization.models import Organization
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from dispatch.project.models import Project
from tests.factories import (
    CaseFactory,
    CasePriorityFactory,
    CaseSeverityFactory,
    CaseTypeFactory,
    IncidentFactory,
    IncidentPriorityFactory,
    IncidentSeverityFactory,
    IncidentTypeFactory,
    OrganizationFactory,
    ParticipantFactory,
    ProjectFactory,
    ServiceFactory,
)


def call_handler(handler, **available):
    """Invoke a handler with only the arguments it declares."""
    wanted = inspect.getfullargspec(inspect.unwrap(handler)).args
    missing = [a for a in wanted if a not in available]
    assert not missing, f"{handler.__name__} needs un-stubbed args: {missing}"
    return handler(**{a: available[a] for a in wanted})


@pytest.fixture
def dispatch_interaction():
    """Run a Slack interaction payload through the real Bolt app.

    Bolt authorizes every request with a live ``auth.test`` call before any
    listener runs, and the app's token is a placeholder -- so that one call has
    to be answered locally or nothing reaches a listener. Everything past it,
    matcher and middleware included, is the real thing.
    """
    from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration
    from dispatch.plugins.dispatch_slack.app import build_app

    # There is no process-global app to dispatch through any more: an App is
    # built per Slack configuration so that no two tenants share one. Building
    # the suite's own gives the listeners a real App without reaching for a
    # tenant's.
    app = build_app(
        SlackConversationConfiguration(
            api_bot_token="xoxb-valid",
            signing_secret="not-a-real-signing-secret-tests-only",
            socket_mode_app_token="xapp-not-real-tests-only",
            app_user_slug="dispatch",
        )
    )

    auth_test = SlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/auth.test",
        req_args={},
        data={
            "ok": True,
            "url": "https://example.slack.com/",
            "team": "Example",
            "user": "dispatch",
            "team_id": "T123",
            "user_id": "UBOT",
            "bot_id": "BBOT",
        },
        headers={},
        status_code=200,
    )

    # Bolt's listener middleware unwind before the listener body rather than
    # wrapping it, so db_middleware's `with get_organization_session(...)` has
    # already closed its session by the time a listener uses it. The listener's
    # first query silently opens a fresh transaction that nothing then closes.
    # A live process gets those backends back when the request is collected;
    # this suite holds its requests, so they would sit open and the session
    # teardown could not drop the database.
    opened: list = []
    real_refetch = database_core.refetch_db_session

    def track(organization_slug: str):
        session = real_refetch(organization_slug)
        opened.append(session)
        return session

    # Action listeners are normally run on a worker thread, so dispatch would
    # return before the handler had done anything to assert on. The listener
    # itself is unaffected by which thread runs it.
    runner = app._listener_runner
    was_synchronous = runner.process_before_response
    runner.process_before_response = True

    try:
        with (
            patch.object(database_core, "refetch_db_session", track),
            patch.object(WebClient, "auth_test", return_value=auth_test),
        ):
            yield lambda body: app.dispatch(BoltRequest(body=body, mode="socket_mode"))
    finally:
        runner.process_before_response = was_synchronous
        for session in opened:
            session.close()


@pytest.fixture
def single_default_organization(session):
    """Leave exactly one organization marked as the default.

    OrganizationFactory randomises the flag, so a suite that has built a few
    of them has several defaults -- and the middleware that resolves a request
    with no subject yet asks for *the* default organization.
    """
    was = {org.id: org.default for org in session.query(Organization).all()}

    for org in session.query(Organization).all():
        org.default = org.slug == "default"
    session.commit()

    yield

    for org in session.query(Organization).all():
        org.default = was.get(org.id, org.default)
    session.commit()


@pytest.fixture
def only_projects(session):
    """Make the set of enabled projects exactly what a test asks for.

    The factories commit, so projects leak from one test into the next -- which
    is why #86 surfaced as an order-dependent failure rather than a reliable
    one. Anything asserting on project select options needs the enabled set to
    be its own, so each call parks whatever is already enabled and returns the
    rows it created. Rows go in one statement: the boundary cases run to a
    thousand projects and a thousand factory commits is not worth the wall
    clock.

    Teardown restores every row it touched, `display_name` included -- a test
    that blanks one is otherwise committed by the factories and outlives the
    rollback.
    """
    original: dict[int, tuple[bool, str]] = {}
    created: list[int] = []

    def remember(project: Project) -> None:
        if project.id not in created and project.id not in original:
            original[project.id] = (project.enabled, project.display_name)

    def make(
        count: int = 0,
        display_names: list[str] | None = None,
        keep: list[Project] = (),
    ) -> list[Project]:
        """Leave `count` projects enabled and return the ones this created.

        Projects in `keep` stay enabled and count toward `count`, for tests
        whose fixtures own a project they still need to be able to select.
        """
        if display_names is not None:
            assert not keep, "keep= and display_names= together would drop the last name"
            count = len(display_names)
        count -= len(keep)

        keep_ids = {p.id for p in keep}
        for project in session.query(Project).filter(Project.enabled.is_(True)).all():
            if project.id in keep_ids:
                continue
            remember(project)
            project.enabled = False

        for project in keep:
            remember(project)
            project.enabled = True

        if count <= 0:
            session.commit()
            return []

        organization = session.query(Organization).order_by(Organization.id).first()
        if organization is None:
            organization = OrganizationFactory()

        prefix = uuid4().hex[:8]
        rows = [
            {
                "name": f"{prefix}-{i:04d}",
                "display_name": (
                    display_names[i] if display_names is not None else f"{prefix}-{i:04d}"
                ),
                "organization_id": organization.id,
                "enabled": True,
                "default": False,
            }
            for i in range(count)
        ]
        ids = session.scalars(insert(Project).returning(Project.id), rows).all()
        session.commit()
        created.extend(ids)

        return session.query(Project).filter(Project.id.in_(ids)).order_by(Project.id).all()

    yield make

    if created:
        session.query(Project).filter(Project.id.in_(created)).delete(synchronize_session=False)
    for project_id, (enabled, display_name) in original.items():
        project = session.get(Project, project_id)
        if project:
            project.enabled = enabled
            project.display_name = display_name
    session.commit()


@pytest.fixture
def incident_with_related_records(session):
    """An incident whose type, severity and priority belong to its own project.

    The select builders look these up by project and return None when a project
    has none, which blockkit rejects as a block. The default IncidentFactory
    gives each sub-object its own project, so the lookups come back empty.
    """
    project = ProjectFactory(display_name="Test Project")
    incident = IncidentFactory(
        project=project,
        incident_type=IncidentTypeFactory(project=project),
        incident_priority=IncidentPriorityFactory(project=project),
        incident_severity=IncidentSeverityFactory(project=project),
        # handle_update_participant_command builds a participant select, which
        # is likewise None when the incident has nobody on it.
        participants=[ParticipantFactory()],
    )
    # handle_engage_oncall_command aborts with CommandError unless the project
    # has at least one oncall service.
    ServiceFactory(project=project, is_active=True)
    return incident


@pytest.fixture
def case_with_related_records(session):
    """A case whose related records all belong to its own project."""
    project = ProjectFactory(display_name="Test Project")
    # Escalation offers incident types and priorities from the same project.
    IncidentTypeFactory(project=project)
    IncidentPriorityFactory(project=project)
    return CaseFactory(
        project=project,
        case_type=CaseTypeFactory(project=project),
        case_priority=CasePriorityFactory(project=project),
        case_severity=CaseSeverityFactory(project=project),
        assignee=ParticipantFactory(),
        status="New",
    )


def _drive(handler, subject_type, subject, session):
    client = MagicMock()
    # Handlers resolve people through Slack; a bare MagicMock is not a str.
    client.users_lookupByEmail.return_value = {"user": {"id": "U999"}}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/p"}

    # BoltContext, not a plain dict: some handlers use attribute access.
    context = BoltContext(
        {
            "subject": SubjectMetadata(
                id=str(subject.id),
                type=subject_type,
                organization_slug="default",
                project_id=str(subject.project.id),
                channel_id="C123",
            ),
            "channel_id": "C123",
            "user_id": "U123",
            "db_session": session,
            "config": MagicMock(),
        }
    )

    call_handler(
        handler,
        ack=MagicMock(),
        body={
            "trigger_id": "T123",
            "user": {"id": "U123"},
            "view": {"id": "V123"},
            "text": "",
            "channel": {"id": "C123"},
            "message": {"ts": "1.0", "text": "a message"},
        },
        client=client,
        context=context,
        db_session=session,
        # Shortcut handlers read the trigger_id off the shortcut, not the body.
        shortcut={"trigger_id": "T123"},
    )

    assert client.views_open.called, f"{handler.__name__} never opened a modal"
    return client.views_open.call_args.kwargs["view"]


@pytest.fixture
def build_incident_modal(session, incident_with_related_records):
    """Run an incident modal handler and return the view it opened."""

    def build(handler):
        return _drive(handler, "incident", incident_with_related_records, session)

    return build


@pytest.fixture
def build_case_modal(session, case_with_related_records):
    """Run a case modal handler and return the view it opened."""

    def build(handler):
        return _drive(handler, "case", case_with_related_records, session)

    return build
