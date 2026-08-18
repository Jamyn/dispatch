"""Argument parsing and project scoping for `/dispatch-list-incidents` (#133).

Both branches of the handler were unreachable. It parsed `payload["command"]`
-- the command's own name, which Bolt only dispatches on when it matches the
registered string exactly -- so the arguments a user typed, which Slack puts in
`payload["text"]`, were never read. And it scoped the listing to the current
incident's project off `context["subject"].type`, which nothing on this
command's middleware chain ever set to anything but `None`.

The organization is deliberately *not* an argument, unlike the pre-Bolt
implementation this restores: the request's own route names the organization
and its signature is verified against that organization's plugin instance, so a
slug in the command text would be a tenant selector that answers to whoever
typed it.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from slack_bolt import Ack, App, BoltContext

import dispatch.plugins.dispatch_slack.middleware as slack_middleware
from dispatch.incident import service as incident_service
from dispatch.project import service as project_service
from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration
from dispatch.plugins.dispatch_slack.exceptions import CommandError, ContextError
from dispatch.plugins.dispatch_slack.incident.interactive import (
    configure,
    handle_list_incidents_command,
    handle_list_tasks_command,
    handle_report_incident_command,
)
from dispatch.plugins.dispatch_slack.middleware import (
    command_context_middleware,
    configuration_middleware,
    optional_command_context_middleware,
    subject_middleware,
)
from dispatch.plugins.dispatch_slack.models import (
    IncidentSubjects,
    SubjectMetadata,
)
from tests.factories import IncidentFactory, ProjectFactory

LIST_INCIDENTS = "/dispatch-list-incidents"


def command_payload(text: str = "", command: str = LIST_INCIDENTS) -> dict:
    """The payload Bolt hands a command listener, with every field Slack sends.

    Both fields the bug confused are populated on purpose: `command` is the
    command's own name and `text` is everything typed after it.
    """
    return {
        "token": "verification-token",
        "team_id": "T123",
        "team_domain": "example",
        "channel_id": "C123",
        "channel_name": "general",
        "user_id": "U123",
        "user_name": "someone",
        "command": command,
        "text": text,
        "api_app_id": "A123",
        "is_enterprise_install": "false",
        "response_url": "https://hooks.slack.com/commands/T123/1/abc",
        "trigger_id": "T123.456.abc",
    }


@pytest.fixture
def run_command(session):
    """Run the handler and report which projects it actually queried.

    The projects are the scoping decision itself, so they are what the scoping
    tests assert on -- the rendered modal caps at 49 incidents and the suite's
    schema accumulates them, so a listing large enough to truncate would hide
    a correct answer.
    """

    def run(payload: dict, subject: SubjectMetadata):
        queried: list[int] = []

        def record(fetch):
            def wrapper(**kwargs):
                queried.append(kwargs["project_id"])
                return fetch(**kwargs)

            return wrapper

        client = MagicMock()
        context = BoltContext(
            {"subject": subject, "channel_id": "C123", "user_id": "U123", "db_session": session}
        )

        with (
            patch.object(
                incident_service,
                "get_all_by_status",
                record(incident_service.get_all_by_status),
            ),
            patch.object(
                incident_service,
                "get_all_last_x_hours_by_status",
                record(incident_service.get_all_last_x_hours_by_status),
            ),
        ):
            handle_list_incidents_command(
                ack=Ack(),
                body={"trigger_id": payload["trigger_id"]},
                payload=payload,
                context=context,
                db_session=session,
                client=client,
            )

        assert client.views_open.called, "the command never opened a modal"
        view = client.views_open.call_args.kwargs["view"]
        # De-duplicated: each project is queried once per status.
        return dict.fromkeys(queried), view

    return run


def unique(label: str) -> str:
    """A project name no other test can already have committed.

    The factories commit, so rows outlive the test that made them and
    `project_service.get_by_name` -- a `one_or_none()` -- would raise on a name
    two tests both chose.
    """
    return f"{label} {uuid4().hex[:8]}"


@pytest.fixture
def two_projects(session):
    """Two projects in one organization, each with an incident of its own."""
    first = ProjectFactory(name=unique("Project Alpha"))
    second = ProjectFactory(name=unique("Project Beta"))
    IncidentFactory(project=first, status="Active")
    IncidentFactory(project=second, status="Active")
    return first, second


def default_subject(slug: str = "default") -> SubjectMetadata:
    """What `subject_middleware` installs when no conversation resolved."""
    return SubjectMetadata(organization_slug=slug)


# --- arguments -------------------------------------------------------------


def test_no_arguments_lists_every_project(run_command, two_projects):
    first, second = two_projects

    queried, view = run_command(command_payload(), default_subject())

    assert first.id in queried
    assert second.id in queried
    assert view["type"] == "modal"


def test_a_project_name_argument_scopes_the_listing(run_command, two_projects):
    """The regression: with `command` parsed instead of `text`, `text` was dead."""
    first, second = two_projects

    queried, view = run_command(command_payload(text=first.name), default_subject())

    assert list(queried) == [first.id]
    assert second.id not in queried


def test_the_listing_shows_the_named_project_s_incidents(run_command, session):
    project = ProjectFactory(name=unique("Project Gamma"))
    incident = IncidentFactory(project=project, status="Active")

    queried, view = run_command(command_payload(text=project.name), default_subject())

    assert list(queried) == [project.id]
    assert incident.name in str(view), "the named project's incident was not listed"


def test_a_project_name_containing_spaces_is_not_split(run_command, session):
    """Project names are not tokens; splitting on whitespace would lose this one."""
    project = ProjectFactory(name=unique("Corporate Security Engineering"))
    IncidentFactory(project=project, status="Active")

    queried, _ = run_command(command_payload(text=project.name), default_subject())

    assert list(queried) == [project.id]


def test_surrounding_whitespace_is_not_a_project_name(run_command, two_projects):
    first, second = two_projects

    queried, _ = run_command(command_payload(text="   "), default_subject())

    assert first.id in queried
    assert second.id in queried


def test_an_unknown_project_name_is_reported_as_such(run_command, session):
    with pytest.raises(CommandError) as excinfo:
        run_command(command_payload(text="No Such Project"), default_subject())

    message = str(excinfo.value)
    assert "No Such Project" in message
    # The old message interpolated `args[0]` -- the command's own name -- as the
    # organization it had looked in.
    assert LIST_INCIDENTS not in message
    assert "default" in message


def test_the_command_name_is_never_parsed_as_an_argument(run_command, two_projects):
    """`payload["command"]` must not be read as arguments even if it could split.

    Bolt dispatches this listener only when `command` equals the registered
    string, so any argument recovered from it was the command's own name.
    """
    first, second = two_projects

    queried, _ = run_command(
        command_payload(text="", command=f"{LIST_INCIDENTS} {first.name}"),
        default_subject(),
    )

    assert first.id in queried
    assert second.id in queried


# --- project scoping -------------------------------------------------------


def test_an_incident_conversation_scopes_to_that_incident_s_project(run_command, two_projects):
    """The branch `subject_middleware` alone could never reach."""
    first, second = two_projects
    incident = IncidentFactory(project=first, status="Active")

    subject = SubjectMetadata(
        type=IncidentSubjects.incident,
        id=str(incident.id),
        organization_slug="default",
        project_id=str(first.id),
    )

    queried, _ = run_command(command_payload(), subject)

    assert list(queried) == [first.id]
    assert second.id not in queried


def test_a_case_conversation_still_lists_the_whole_organization(run_command, two_projects):
    """Only an incident subject narrows the listing; a case is not an incident."""
    first, second = two_projects

    queried, _ = run_command(command_payload(), SubjectMetadata(type="case", id="1"))

    assert first.id in queried
    assert second.id in queried


# --- organization scoping --------------------------------------------------


def test_the_organization_comes_from_the_subject_not_the_text(run_command, session):
    """A slug in the command text is a project name, never a tenant selector.

    The pre-Bolt command took `<organization> <project>`. Restoring that would
    let anyone who can type into Slack name the tenant to read, so the whole
    string is a project name in the organization the request already belongs to.
    """
    other = ProjectFactory(name=unique("Project Delta"))
    IncidentFactory(project=other, status="Active")

    with pytest.raises(CommandError) as excinfo:
        run_command(
            command_payload(text=f"some-other-org {other.name}"),
            default_subject(),
        )

    message = str(excinfo.value)
    assert f"some-other-org {other.name}" in message
    assert "in organization 'default' not found" in message


def test_the_project_lookup_runs_on_the_request_s_own_session(run_command, two_projects, session):
    """The session is the organization boundary, and the text cannot pick one.

    A lookup on any session but the one the middleware bound to this request
    would be reading a tenant the request does not belong to.
    """
    first, _ = two_projects
    sessions = []
    real = project_service.get_by_name

    def spy(**kwargs):
        sessions.append(kwargs["db_session"])
        return real(**kwargs)

    with patch.object(project_service, "get_by_name", spy):
        run_command(command_payload(text=first.name), default_subject())

    assert sessions == [session]


# --- registration and middleware ------------------------------------------


def build_configured_app() -> App:
    app = App(
        token="xoxb-not-real-tests-only",
        signing_secret="not-a-real-signing-secret-tests-only",
        request_verification_enabled=False,
        token_verification_enabled=False,
    )
    configure(
        app,
        SlackConversationConfiguration(
            api_bot_token="xoxb-not-real-tests-only",
            signing_secret="not-a-real-signing-secret-tests-only",
            socket_mode_app_token="xapp-not-real-tests-only",
            app_user_slug="dispatch",
        ),
    )
    return app


def middleware_for(app: App, handler) -> list:
    """The middleware functions a listener was registered with, in order."""
    for listener in app._listeners:
        if listener.ack_function is handler:
            # Unwrap the `partial` that binds `expected_subject`.
            return [getattr(m.func, "func", m.func) for m in listener.middleware]
    raise AssertionError(f"{handler.__name__} is not registered")


@pytest.fixture(scope="module")
def configured_app() -> App:
    return build_configured_app()


def test_list_incidents_resolves_the_conversation_it_was_run_in(configured_app):
    assert middleware_for(configured_app, handle_list_incidents_command) == [
        subject_middleware,
        configuration_middleware,
        optional_command_context_middleware,
    ]


def test_list_incidents_does_not_require_a_channel(configured_app):
    """`command_context_middleware` would reject the command outside a channel."""
    assert command_context_middleware not in middleware_for(
        configured_app, handle_list_incidents_command
    )


def test_report_incident_is_unaffected(configured_app):
    assert middleware_for(configured_app, handle_report_incident_command) == [
        subject_middleware,
        configuration_middleware,
    ]


def test_incident_scoped_commands_still_require_an_incident_channel(configured_app):
    assert command_context_middleware in middleware_for(configured_app, handle_list_tasks_command)


def test_the_middleware_adopts_the_conversation_s_organization_and_session():
    conversation = SubjectMetadata(
        type=IncidentSubjects.incident,
        id="7",
        organization_slug="some-other-tenant",
        project_id="3",
    )
    context = BoltContext(
        {"subject": default_subject(), "channel_id": "C123", "db_session": "default-session"}
    )
    ran = []

    with patch.object(
        slack_middleware,
        "resolve_context_from_conversation",
        return_value=slack_middleware.Subject(conversation, db_session="tenant-session"),
    ):
        optional_command_context_middleware(context=context, next=lambda: ran.append(True))

    assert ran
    assert context["subject"] is conversation
    assert context["db_session"] == "tenant-session"


def test_the_middleware_falls_through_outside_a_channel():
    subject = default_subject()
    context = BoltContext(
        {"subject": subject, "channel_id": "C999", "db_session": "default-session"}
    )
    ran = []

    with patch.object(slack_middleware, "resolve_context_from_conversation", return_value=None):
        optional_command_context_middleware(context=context, next=lambda: ran.append(True))

    assert ran
    assert context["subject"] is subject
    assert context["db_session"] == "default-session"


def test_the_required_middleware_would_have_rejected_the_same_conversation():
    """Why the command gets the optional variant and not `command_context_middleware`."""
    context = BoltContext({"channel_id": "C999"})

    with patch.object(slack_middleware, "resolve_context_from_conversation", return_value=None):
        with pytest.raises(ContextError):
            command_context_middleware(
                context=context, payload=command_payload(), next=lambda: None
            )
