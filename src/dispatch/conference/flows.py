import logging

from dispatch.database.core import SessionLocal
from dispatch.enums import EventType
from dispatch.event import service as event_service
from dispatch.incident.models import Incident
from dispatch.plugin import service as plugin_service

from .models import ConferenceCreate
from .service import create

log = logging.getLogger(__name__)


def update_conference_participant(
    incident: Incident, participant_email: str, db_session: SessionLocal, remove: bool
):
    """Add or remove a participant on the incident's conference roster.

    Roster metadata only -- neither platform gates joining on it, so this grants
    no access and revokes none. Failures are recorded and dropped: getting a
    responder into the tactical group and the conversation is what actually
    matters, and losing that because a roster update failed would be a
    regression. The exception handling is scoped to the plugin call alone.
    """
    if not incident.conference:
        log.debug("Conference participants not updated. No conference for this incident.")
        return

    plugin = plugin_service.get_active_instance(
        db_session=db_session, project_id=incident.project.id, plugin_type="conference"
    )
    if not plugin:
        log.warning("Conference participants not updated. No conference plugin enabled.")
        return

    action = "removed from" if remove else "added to"

    try:
        if remove:
            plugin.instance.remove_participant(incident.conference.conference_id, participant_email)
        else:
            plugin.instance.add_participant(incident.conference.conference_id, participant_email)
    except Exception as e:
        event_service.log_incident_event(
            db_session=db_session,
            source="Dispatch Core App",
            description=f"{participant_email} could not be {action} the incident conference. Reason: {e}",
            incident_id=incident.id,
            type=EventType.participant_updated,
        )
        log.exception(e)
        return

    log.info(f"Participant {action} the incident conference (incident ID: {incident.id}).")


def add_conference_participant(
    incident: Incident, participant_email: str, db_session: SessionLocal
):
    """Adds a participant to the incident conference roster."""
    update_conference_participant(incident, participant_email, db_session, remove=False)


def remove_conference_participant(
    incident: Incident, participant_email: str, db_session: SessionLocal
):
    """Removes a participant from the incident conference roster."""
    update_conference_participant(incident, participant_email, db_session, remove=True)


def create_conference(incident: Incident, participants: list[str], db_session: SessionLocal):
    """Creates a conference room."""
    plugin = plugin_service.get_active_instance(
        db_session=db_session, project_id=incident.project.id, plugin_type="conference"
    )
    if not plugin:
        log.warning("Conference room not created. No conference plugin enabled.")
        return

    # we create the external conference room
    try:
        external_conference = plugin.instance.create(
            incident.name, title=incident.title, participants=participants
        )
    except Exception as e:
        event_service.log_incident_event(
            db_session=db_session,
            source="Dispatch Core App",
            description=f"Creating the incident conference room failed. Reason: {e}",
            incident_id=incident.id,
        )
        log.exception(e)
        return

    if not external_conference:
        log.error(f"Conference not created. Plugin {plugin.plugin.slug} encountered an error.")
        return

    external_conference.update(
        {"resource_type": plugin.plugin.slug, "resource_id": external_conference["id"]}
    )

    # we create the internal conference room
    conference_in = ConferenceCreate(
        resource_id=external_conference["resource_id"],
        resource_type=external_conference["resource_type"],
        weblink=external_conference["weblink"],
        conference_id=external_conference["id"],
        conference_challenge=external_conference["challenge"],
    )
    conference = create(conference_in=conference_in, db_session=db_session)
    incident.conference = conference

    db_session.add(incident)
    db_session.commit()

    event_service.log_incident_event(
        db_session=db_session,
        source=plugin.plugin.title,
        description="Incident conference created",
        incident_id=incident.id,
    )

    return conference
