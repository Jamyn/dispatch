import logging
from datetime import timedelta
from sqlalchemy.orm import Session

from blockkit import (
    Checkboxes,
    DatePicker,
    ExternalSelect,
    Input,
    MultiExternalSelect,
    MultiStaticSelect,
    Option,
    PlainTextInput,
    StaticSelect,
)

from dispatch.case.enums import CaseResolutionReason, CaseStatus
from dispatch.case.priority import service as case_priority_service
from dispatch.case.severity import service as case_severity_service
from dispatch.case.type import service as case_type_service
from dispatch.entity import service as entity_service
from dispatch.enums import DispatchEnum, Visibility
from dispatch.incident.enums import IncidentStatus
from dispatch.incident.priority import service as incident_priority_service
from dispatch.incident.severity import service as incident_severity_service
from dispatch.incident.type import service as incident_type_service
from dispatch.participant.models import Participant
from dispatch.plugins.dispatch_slack.config import (
    MAX_OPTION_TEXT_LENGTH,
    MAX_SECTION_TEXT_LENGTH,
    MAX_SELECT_OPTIONS,
)
from dispatch.project import service as project_service
from dispatch.project.models import Project
from dispatch.signal.models import Signal

log = logging.getLogger(__name__)


class DefaultBlockIds(DispatchEnum):
    add_user_actions = "add-user-actions"
    date_picker_input = "date-picker-input"
    description_input = "description-input"
    hour_picker_input = "hour-picker-input"
    minute_picker_input = "minute-picker-input"
    project_select = "project-select"
    relative_date_picker_input = "relative-date-picker-input"
    resolution_input = "resolution-input"
    timezone_picker_input = "timezone-picker-input"
    title_input = "title-input"

    # incidents
    incident_priority_select = "incident-priority-select"
    incident_status_select = "incident-status-select"
    incident_severity_select = "incident-severity-select"
    incident_type_select = "incident-type-select"

    # cases
    case_priority_select = "case-priority-select"
    case_resolution_reason_select = "case-resolution-reason-select"
    case_status_select = "case-status-select"
    case_severity_select = "case-severity-select"
    case_type_select = "case-type-select"
    case_visibility_select = "case-visibility-select"
    case_assignee_select = "case-assignee-select"

    # entities
    entity_select = "entity-select"

    # participants
    participant_select = "participant-select"

    # signals
    signal_definition_select = "signal-definition-select"
    extension_request_checkbox = "extension_request_checkbox"

    # tags
    tags_multi_select = "tag-multi-select"


class DefaultActionIds(DispatchEnum):
    date_picker_input = "date-picker-input"
    description_input = "description-input"
    hour_picker_input = "hour-picker-input"
    minute_picker_input = "minute-picker-input"
    project_select = "project-select"
    relative_date_picker_input = "relative-date-picker-input"
    resolution_input = "resolution-input"
    timezone_picker_input = "timezone-picker-input"
    title_input = "title-input"

    # incidents
    incident_priority_select = "incident-priority-select"
    incident_status_select = "incident-status-select"
    incident_severity_select = "incident-severity-select"
    incident_type_select = "incident-type-select"

    # cases
    case_resolution_reason_select = "case-resolution-reason-select"
    case_priority_select = "case-priority-select"
    case_status_select = "case-status-select"
    case_severity_select = "case-severity-select"
    case_type_select = "case-type-select"
    case_visibility_select = "case-visibility-select"

    # entities
    entity_select = "entity-select"

    # participants
    participant_select = "participant-select"

    # signals
    signal_definition_select = "signal-definition-select"
    extension_request_checkbox = "extension_request_checkbox"

    # tags
    tags_multi_select = "tag-multi-select"


class TimezoneOptions(DispatchEnum):
    local = "Local Time (based on your Slack profile)"
    utc = "UTC"


def relative_date_picker_input(
    action_id: str = DefaultActionIds.relative_date_picker_input,
    block_id: str = DefaultBlockIds.relative_date_picker_input,
    initial_option: dict = None,
    label: str = "Date",
    **kwargs,
):
    """Builds a relative date picker input."""
    relative_dates = [
        {"text": "1 hour", "value": str(timedelta(hours=1))},
        {"text": "3 hours", "value": str(timedelta(hours=3))},
        {"text": "1 day", "value": str(timedelta(days=1))},
        {"text": "3 days", "value": str(timedelta(days=3))},
        {"text": "1 week", "value": str(timedelta(weeks=1))},
        {"text": "2 weeks", "value": str(timedelta(weeks=2))},
    ]

    return static_select_block(
        action_id=action_id,
        block_id=block_id,
        initial_option=initial_option,
        options=relative_dates,
        label=label,
        placeholder="Relative Time",
        **kwargs,
    )


def date_picker_input(
    action_id: str = DefaultActionIds.date_picker_input,
    block_id: str = DefaultBlockIds.date_picker_input,
    initial_date: str = None,
    label: str = "Date",
    **kwargs,
):
    """Builds a date picker input."""
    return Input(
        element=DatePicker(
            action_id=action_id, initial_date=initial_date, placeholder="Select Date"
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def hour_picker_input(
    action_id: str = DefaultActionIds.hour_picker_input,
    block_id: str = DefaultBlockIds.hour_picker_input,
    initial_option: dict = None,
    label: str = "Hour",
    **kwargs,
):
    """Builds an hour picker input."""
    hours = [{"text": str(h).zfill(2), "value": str(h).zfill(2)} for h in range(0, 24)]
    return static_select_block(
        action_id=action_id,
        block_id=block_id,
        initial_option=initial_option,
        options=hours,
        label=label,
        placeholder="Hour",
    )


def minute_picker_input(
    action_id: str = DefaultActionIds.minute_picker_input,
    block_id: str = DefaultBlockIds.minute_picker_input,
    initial_option: dict = None,
    label: str = "Minute",
    **kwargs,
):
    """Builds a minute picker input."""
    minutes = [{"text": str(m).zfill(2), "value": str(m).zfill(2)} for m in range(0, 60)]
    return static_select_block(
        action_id=action_id,
        block_id=block_id,
        initial_option=initial_option,
        options=minutes,
        label=label,
        placeholder="Minute",
    )


def timezone_picker_input(
    action_id: str = DefaultActionIds.timezone_picker_input,
    block_id: str = DefaultBlockIds.timezone_picker_input,
    initial_option: dict = None,
    label: str = "Timezone",
    **kwargs,
):
    """Builds a timezone picker input."""
    if not initial_option:
        initial_option = {
            "text": TimezoneOptions.local.value,
            "value": TimezoneOptions.local.value,
        }
    return static_select_block(
        action_id=action_id,
        block_id=block_id,
        initial_option=initial_option,
        options=[{"text": tz.value, "value": tz.value} for tz in TimezoneOptions],
        label=label,
        placeholder="Timezone",
    )


def datetime_picker_block(
    action_id: str = None,
    block_id: str = None,
    initial_option: str = None,
    label: str = None,
    **kwargs,
):
    """Builds a datetime picker block"""
    hour = None
    minute = None
    date = initial_option.split("|")[0] if initial_option.split("|")[0] != "" else None

    if initial_option.split("|")[1] != "":
        # appends zero if time is not entered in hh format
        if len(initial_option.split("|")[1].split(":")[0]) == 1:
            h = "0" + initial_option.split("|")[1].split(":")[0]
        else:
            h = initial_option.split("|")[1].split(":")[0]
        hour = {"text": h, "value": h}
        minute = {
            "text": initial_option.split("|")[1].split(":")[1],
            "value": initial_option.split("|")[1].split(":")[1],
        }
    return [
        date_picker_input(initial_date=date),
        hour_picker_input(initial_option=hour),
        minute_picker_input(initial_option=minute),
        timezone_picker_input(),
    ]


def static_select_block(
    options: list[dict[str, str]],
    placeholder: str,
    action_id: str = None,
    block_id: str = None,
    initial_option: dict[str, str] = None,
    label: str = None,
    **kwargs,
):
    """Builds a static select block."""
    # Ensure all values in options are strings
    processed_options = []
    if options:
        for x in options:
            option_dict = {k: str(v) if k == "value" else v for k, v in x.items()}
            processed_options.append(option_dict)

    # Ensure value in initial_option is a string
    processed_initial_option = None
    if initial_option:
        processed_initial_option = {
            k: str(v) if k == "value" else v for k, v in initial_option.items()
        }

    return Input(
        element=StaticSelect(
            placeholder=placeholder,
            options=[Option(**x) for x in processed_options] if processed_options else None,
            initial_option=(
                Option(**processed_initial_option) if processed_initial_option else None
            ),
            action_id=action_id,
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def multi_select_block(
    options: list[dict[str, str]],
    placeholder: str,
    action_id: str = None,
    block_id: str = None,
    label: str = None,
    **kwargs,
):
    """Builds a multi select block."""
    # Ensure all values in options are strings
    processed_options = []
    if options:
        for x in options:
            option_dict = {k: str(v) if k == "value" else v for k, v in x.items()}
            processed_options.append(option_dict)

    return Input(
        element=MultiStaticSelect(
            placeholder=placeholder,
            options=[Option(**x) for x in processed_options] if processed_options else None,
            action_id=action_id,
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def external_select_block(
    placeholder: str,
    action_id: str = None,
    block_id: str = None,
    initial_option: dict[str, str] = None,
    label: str = None,
    # Slack's own default is 3 characters. One keeps the menu responsive on a
    # short project name while still bounding how much a keystroke costs. It is
    # also the floor the blockkit library imposes -- Slack itself accepts 0,
    # but blockkit refuses to build it, and 0 would buy nothing here: Slack
    # loads the menu on open regardless, and this only gates typing.
    min_query_length: int = 1,
    **kwargs,
):
    """Builds an external select block, whose options are loaded on demand.

    Slack asks for the options over the block_suggestion route rather than
    reading them out of the view, so nothing here is bounded by the 100-option
    limit a static select carries.
    """
    processed_initial_option = None
    if initial_option:
        processed_initial_option = {
            k: str(v) if k == "value" else v for k, v in initial_option.items()
        }

    return Input(
        element=ExternalSelect(
            placeholder=placeholder,
            initial_option=(
                Option(**processed_initial_option) if processed_initial_option else None
            ),
            min_query_length=min_query_length,
            action_id=action_id,
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def project_label(project: Project) -> str:
    """The option text for a project, within Slack's length limit.

    Never empty, because Slack rejects an option with empty text and this is
    used for the preselected option too, where no query has filtered anything
    out. ``display_name`` is only defaulted to '' at the column level, so a
    project created without one falls back to its name -- the same pairing the
    rest of Dispatch lists a project under, and what the migration that added
    the column seeded it from. Falling back past that to the id keeps a
    project whose name is null selectable rather than dropping it from the
    menu; it cannot be found by typing, since there is nothing to match on.
    """
    label = project.display_name or project.name or f"Project {project.id}"
    return label[:MAX_OPTION_TEXT_LENGTH]


def project_option(project: Project) -> dict[str, str]:
    """Builds the select option for a project.

    The text is what a project is called everywhere else it is listed; the
    value is its id, which is what every handler reads back. They are not
    interchangeable -- two projects may share a display name.
    """
    return {
        "text": project_label(project),
        "value": str(project.id),
    }


def project_select(
    db_session: Session,
    action_id: str = DefaultActionIds.project_select,
    block_id: str = DefaultBlockIds.project_select,
    label: str = "Project",
    initial_option: dict = None,
    **kwargs,
):
    """Creates a project select.

    Embedding the options only works while there are few enough of them to
    embed, so past Slack's limit this hands over to an external select and the
    options handler in ``.options``. Both produce the same block and action
    ids and the same ``selected_option`` shape, so callers cannot tell which
    one they got.
    """
    # One row past the limit is all it takes to know which of the two applies,
    # and is far cheaper than counting a large project table.
    projects = project_service.get_all_enabled(db_session=db_session, limit=MAX_SELECT_OPTIONS + 1)
    if not projects:
        log.warning("Unable to create a select block for projects. No projects found.")
        return

    if len(projects) > MAX_SELECT_OPTIONS:
        return external_select_block(
            # Slack loads the menu on open, so this is still a browse; past a
            # hundred projects it is also worth saying that typing narrows it.
            placeholder="Select or search for a project",
            initial_option=initial_option,
            action_id=action_id,
            block_id=block_id,
            label=label,
            **kwargs,
        )

    return static_select_block(
        placeholder="Select Project",
        options=[project_option(p) for p in projects],
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def title_input(
    label: str = "Title",
    placeholder: str = "A brief explanatory title. You can change this later.",
    action_id: str = DefaultActionIds.title_input,
    block_id: str = DefaultBlockIds.title_input,
    initial_value: str = None,
    **kwargs,
):
    """Builds a title input."""
    return Input(
        element=PlainTextInput(
            placeholder=placeholder,
            initial_value=initial_value,
            action_id=action_id,
            max_length=MAX_SECTION_TEXT_LENGTH,
        ),
        label=label,
        block_id=block_id,
        **kwargs,
    )


def description_input(
    label: str = "Description",
    placeholder: str = "A summary of what you know so far. It's okay if this is incomplete.",
    action_id: str = DefaultActionIds.description_input,
    block_id: str = DefaultBlockIds.description_input,
    initial_value: str = None,
    **kwargs,
):
    """Builds a description input."""
    return Input(
        element=PlainTextInput(
            placeholder=placeholder,
            initial_value=initial_value,
            multiline=True,
            action_id=action_id,
            max_length=MAX_SECTION_TEXT_LENGTH,
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def resolution_input(
    label: str = "Resolution",
    action_id: str = DefaultActionIds.resolution_input,
    block_id: str = DefaultBlockIds.resolution_input,
    initial_value: str = None,
    **kwargs,
):
    """Builds a resolution input."""
    return Input(
        element=PlainTextInput(
            placeholder="A description of the actions you have taken toward resolution.",
            initial_value=initial_value,
            multiline=True,
            action_id=action_id,
            max_length=MAX_SECTION_TEXT_LENGTH,
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def case_resolution_reason_select(
    action_id: str = DefaultActionIds.case_resolution_reason_select,
    block_id: str = DefaultBlockIds.case_resolution_reason_select,
    label: str = "Resolution Reason",
    initial_option: dict = None,
    **kwargs,
):
    """Creates an incident priority select."""
    reasons = [{"text": str(s), "value": str(s)} for s in CaseResolutionReason]

    return static_select_block(
        placeholder="Select Resolution Reason",
        options=reasons,
        initial_option=initial_option,
        block_id=block_id,
        action_id=action_id,
        label=label,
        **kwargs,
    )


def incident_status_select(
    block_id: str = DefaultActionIds.incident_status_select,
    action_id: str = DefaultBlockIds.incident_status_select,
    label: str = "Incident Status",
    initial_option: dict = None,
    **kwargs,
):
    """Creates an incident status select."""
    statuses = [{"text": s.value, "value": s.value} for s in IncidentStatus]
    return static_select_block(
        placeholder="Select Status",
        options=statuses,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def incident_priority_select(
    db_session: Session,
    action_id: str = DefaultActionIds.incident_priority_select,
    block_id: str = DefaultBlockIds.incident_priority_select,
    label: str = "Incident Priority",
    initial_option: dict = None,
    project_id: int = None,
    **kwargs,
):
    """Creates an incident priority select."""
    priorities = [
        {"text": p.name, "value": p.id}
        for p in incident_priority_service.get_all_enabled(
            db_session=db_session, project_id=project_id
        )
    ]
    if not priorities:
        log.warning(
            "Unable to create a select block for incident priorities. No incident priorities found."
        )
        return

    return static_select_block(
        placeholder="Select Priority",
        options=priorities,
        initial_option=initial_option,
        block_id=block_id,
        action_id=action_id,
        label=label,
        **kwargs,
    )


def incident_severity_select(
    db_session: Session,
    action_id: str = DefaultActionIds.incident_severity_select,
    block_id: str = DefaultBlockIds.incident_severity_select,
    label="Incident Severity",
    initial_option: dict = None,
    project_id: int = None,
    **kwargs,
):
    """Creates an incident severity select."""
    severities = [
        {"text": s.name, "value": s.id}
        for s in incident_severity_service.get_all_enabled(
            db_session=db_session, project_id=project_id
        )
    ]
    if not severities:
        log.warning(
            "Unable to create a select block for incident severities. No incident severities found."
        )
        return

    return static_select_block(
        placeholder="Select Severity",
        options=severities,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def incident_type_select(
    db_session: Session,
    action_id: str = DefaultActionIds.incident_type_select,
    block_id: str = DefaultBlockIds.incident_type_select,
    label="Incident Type",
    initial_option: dict = None,
    project_id: int = None,
    **kwargs,
):
    """Creates an incident type select."""
    types = [
        {"text": t.name, "value": t.id}
        for t in incident_type_service.get_all_enabled(db_session=db_session, project_id=project_id)
    ]
    if not types:
        log.warning("Unable to create a select block for incident types. No incident types found.")
        return

    return static_select_block(
        placeholder="Select Type",
        options=types,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def tag_multi_select(
    action_id: str = DefaultActionIds.tags_multi_select,
    block_id: str = DefaultBlockIds.tags_multi_select,
    label="Tags",
    initial_options: str = None,
    **kwargs,
):
    """Creates an incident tag select."""
    return Input(
        element=MultiExternalSelect(
            placeholder="Select Tag(s)", action_id=action_id, initial_options=initial_options
        ),
        block_id=block_id,
        label=label,
        **kwargs,
    )


def case_status_select(
    action_id: str = DefaultActionIds.case_status_select,
    block_id: str = DefaultBlockIds.case_status_select,
    label: str = "Status",
    initial_option: dict | None = None,
    statuses: list[dict[str, str]] | None = None,
    **kwargs,
):
    """Creates a case status select."""
    if not statuses:
        statuses = [{"text": str(s), "value": str(s)} for s in CaseStatus]

    return static_select_block(
        placeholder="Select Status",
        options=statuses,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def case_priority_select(
    db_session: Session,
    action_id: str = DefaultActionIds.case_priority_select,
    block_id: str = DefaultBlockIds.case_priority_select,
    label="Case Priority",
    initial_option: dict = None,
    project_id: int = None,
    **kwargs,
):
    """Creates a case priority select."""
    priorities = [
        {"text": p.name, "value": p.id}
        for p in case_priority_service.get_all_enabled(db_session=db_session, project_id=project_id)
    ]
    if not priorities:
        log.warning(
            "Unable to create a select block for case priorities. No case priorities found."
        )
        return

    return static_select_block(
        placeholder="Select Priority",
        options=priorities,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def case_severity_select(
    db_session: Session,
    action_id: str = DefaultActionIds.case_severity_select,
    block_id: str = DefaultBlockIds.case_severity_select,
    label: str = "Case Severity",
    initial_option: dict = None,
    project_id: int = None,
    **kwargs,
):
    """Creates a case severity select."""
    severities = [
        {"text": s.name, "value": s.id}
        for s in case_severity_service.get_all_enabled(db_session=db_session, project_id=project_id)
    ]
    if not severities:
        log.warning(
            "Unable to create a select block for case severities. No case severities found."
        )
        return

    return static_select_block(
        placeholder="Select Severity",
        options=severities,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def case_type_select(
    db_session: Session,
    action_id: str = DefaultActionIds.case_type_select,
    block_id: str = DefaultBlockIds.case_type_select,
    label: str = "Case Type",
    initial_option: dict | None = None,
    project_id: int = None,
    **kwargs,
):
    """Creates a case type select."""
    types = [
        {"text": t.name, "value": t.id}
        for t in case_type_service.get_all_enabled(db_session=db_session, project_id=project_id)
    ]

    if not types:
        log.warning("Unable to create a select block for case types. No case types found.")
        return

    return static_select_block(
        placeholder="Select Type",
        options=types,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def case_visibility_select(
    action_id: str = DefaultActionIds.case_visibility_select,
    block_id: str = DefaultBlockIds.case_visibility_select,
    label: str = "Case Visibility",
    initial_option: dict | None = None,
    **kwargs,
):
    """Creates a case visibility select."""
    visibility = [
        {"text": Visibility.restricted, "value": Visibility.restricted},
        {"text": Visibility.open, "value": Visibility.open},
    ]

    return static_select_block(
        placeholder="Select Visibility",
        options=visibility,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def entity_select(
    signal_id: int,
    db_session: Session,
    action_id: str = DefaultActionIds.entity_select,
    block_id: str = DefaultBlockIds.entity_select,
    label="Entities",
    case_id: int = None,
    **kwargs,
):
    """Creates an entity select."""
    entity_options = [
        {"text": entity.value[:75], "value": entity.id}
        for entity in entity_service.get_all_desc_by_signal(
            db_session=db_session, signal_id=signal_id, case_id=case_id
        )
        if entity.value
    ]

    if not entity_options:
        log.warning("Unable to create a select block for entities. No entities found.")
        return

    return multi_select_block(
        placeholder="Select Entities",
        options=entity_options[:100],  # Limit the entities to the first 100 most recent
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def participant_select(
    participants: list[Participant],
    action_id: str = DefaultActionIds.participant_select,
    block_id: str = DefaultBlockIds.participant_select,
    label: str = "Participant",
    initial_option: Participant = None,
    **kwargs,
):
    """Creates a static select of available participants."""
    participants = [{"text": p.individual.name, "value": p.id} for p in participants]
    if not participants:
        log.warning("Unable to create a select block for participants. No participants found.")
        return

    return static_select_block(
        placeholder="Select Participant",
        options=participants,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def signal_definition_select(
    signals: list[Signal],
    action_id: str = DefaultActionIds.signal_definition_select,
    block_id: str = DefaultBlockIds.signal_definition_select,
    label: str = "Signal Definitions",
    initial_option: Participant = None,
    **kwargs,
):
    """Creates a static select of available signal definitions."""
    signals = [{"text": s.name, "value": s.id} for s in signals]
    if not signals:
        log.warning(
            "Unable to create a select block for signal definitions. No signals definitions found."
        )
        return

    return static_select_block(
        placeholder="Select Signal Definition",
        options=signals,
        initial_option=initial_option,
        action_id=action_id,
        block_id=block_id,
        label=label,
        **kwargs,
    )


def extension_request_checkbox(
    action_id: str = DefaultActionIds.extension_request_checkbox,
    block_id: str = DefaultBlockIds.extension_request_checkbox,
    label: str = "Request longer expiration",
    **kwargs,
):
    options = [
        Option(
            text=("Check this box to request an expiration longer than 2 weeks."),
            value="Yes",
        )
    ]
    return Input(
        block_id=block_id,
        element=Checkboxes(options=options, action_id=action_id),
        label=label,
        optional=True,
        **kwargs,
    )
