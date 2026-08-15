import logging

from dispatch.database.core import SessionLocal
from dispatch.enums import EventType
from dispatch.event import service as event_service
from dispatch.exceptions import ConferenceCreatedButUnusable
from dispatch.incident.models import Incident
from dispatch.plugin import service as plugin_service

from .models import Conference, ConferenceCreate
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


def dispatch_owns_conference(db_session: SessionLocal, incident_id: int, resource_id) -> bool:
    """Positive proof that this incident ended up owning this meeting.

    A COMMIT whose acknowledgement is lost on the way back raises while the row
    is durably written. Deleting then would destroy a bridge the incident really
    owns, and nothing would replace it: `incident_create_resources_flow` guards
    on `if not incident.conference`, which that row satisfies.

    Matched on the incident *and* the provider id, which is the question that
    actually matters -- and `conference.resource_id` carries no unique
    constraint, so a bare lookup by it can find a row belonging to someone else
    or refuse to answer at all.

    Only a positive answer withholds the delete. An absent row, a session that
    will not answer and an unreachable database all count as "not proven",
    because the two mistakes are not symmetric -- a deleted bridge leaves a dead
    link an operator can see and recreate, while a kept one leaves a live
    meeting nobody can ever find. So this can only ever prevent a deletion it is
    sure about; it never causes one.
    """
    if not resource_id:
        return False

    try:
        # The session refuses everything until it is rolled back once a flush has
        # failed, and the caller's transaction is lost either way --
        # `background_task` rolls back behind us on the paths that own a session.
        db_session.rollback()
        return (
            db_session.query(Conference.id)
            .filter(
                Conference.incident_id == incident_id,
                Conference.resource_id == str(resource_id),
            )
            .first()
            is not None
        )
    except Exception as e:
        log.warning(
            "Could not establish whether conference %s was persisted, so it is treated as "
            "unowned. Reason: %s",
            resource_id,
            e,
        )
        return False


def delete_unowned_conference(conference_plugin, resource_id, original_error: Exception):
    """Delete a provider meeting Dispatch created and then failed to take over.

    Compensation, not teardown. It runs only inside the window between the
    provider accepting a meeting and Dispatch committing the row that owns it,
    and that window is most of the justification: with no `Conference` row there
    is no `incident.conference`, and `incident_delete_flow` has no other way to
    reach a bridge, so leaving it is permanent (issue #114). On Zoom and Teams
    nothing has published the join link at this point either -- `create_conference`
    is what puts it on the incident -- so the meeting has no users to strand.
    Google Calendar is the exception: it invites attendees at insert time, and
    there the delete is what withdraws them, which still beats leaving
    responders a bridge the incident does not know about.

    Deletes by the id the provider returned from this same create call and by
    nothing else. Never looks a conference up by name, title or weblink: those
    are not unique and a match could be a bridge belonging to a live incident.

    Takes the *resolved* plugin, not the `PluginInstance` row, and touches no
    session. `PluginInstance.instance` lazy-loads `self.plugin`, which raises on
    a session a failed flush has left needing a rollback -- and its own handler
    then dies on a `self.slug` the row does not have. Reading it here would lose
    the plugin in the very case compensation exists for.

    A plugin that does not implement `delete` raises `NotImplementedError` from
    the base class into the handler below, and is reported like any other failed
    cleanup rather than replacing the error that got us here.
    """
    if not resource_id:
        # Nothing safe to target, and nothing else identifies the meeting. This
        # log line is the only trace the leak will ever get, so it is an error.
        log.error(
            "The conference provider may be holding a meeting Dispatch never owned. "
            "It returned no id, so there is nothing to delete it by. Cause: %s",
            type(original_error).__name__,
        )
        return

    try:
        conference_plugin.delete(resource_id)
    except Exception as e:
        # Logged here and never raised: the caller reports the failure that made
        # cleanup necessary, and letting a failed DELETE replace a database error
        # would hide the actual fault behind its consequence.
        #
        # No `exc_info`. This runs inside the original failure's own `except`,
        # so Python has set `__context__` and a traceback would print that
        # exception too -- and a SQLAlchemy DBAPI error stringifies with its
        # bound parameters, which here are the weblink (Zoom puts the passcode
        # in `?pwd=`) and the challenge. The caller re-raises the original with
        # its own traceback, which is where that detail belongs.
        log.error(
            "Orphaned conference %s could not be deleted, and is now unreachable. Reason: %s",
            resource_id,
            e,
        )
        return

    # Only the id is named, for the same reason.
    log.warning(
        "Conference %s was deleted: the provider created it but Dispatch could not take "
        "ownership of it. Cause: %s",
        resource_id,
        type(original_error).__name__,
    )


def create_conference(incident: Incident, participants: list[str], db_session: SessionLocal):
    """Creates a conference room.

    The provider commits a meeting before Dispatch commits anything, so every
    step between those two points can leave a live bridge with no database row
    behind it -- unreachable forever, since teardown only ever finds a
    conference through `incident.conference` (issue #114). Two things close
    that window, and both are load-bearing:

    - the row and its incident are one commit, so no failure can persist a
      parentless `Conference`; and
    - from the moment the provider accepts a meeting until that commit returns,
      any `Exception` deletes the meeting again -- unless the row turns out to
      have landed after all, which a raised COMMIT does not rule out.

    The compensation boundary is *ownership*, not the kind of failure. A
    provider error deletes nothing because there is nothing to delete; a bug in
    Dispatch does delete, because a meeting Dispatch cannot persist is a meeting
    nobody can reach or ever clean up, whoever is at fault. Which is also why
    the guarded span stops at the commit rather than at the end of the function:
    past that point the bridge is owned, and deleting it would destroy something
    real.

    Four residual leaks remain, none of them closable here. A plugin that fails
    after the provider accepted a meeting is only compensated if it raises
    `ConferenceCreatedButUnusable`, which the base class cannot force on a
    plugin shipped elsewhere. A `BaseException` -- a shutdown signal, say -- is
    deliberately not caught. The compensating delete can itself fail. And the
    process can die between the provider's create and that delete; closing that
    one needs an intent record written before the provider is called, plus a
    sweeper to act on it, which is a much larger design than this.
    """
    plugin = plugin_service.get_active_instance(
        db_session=db_session, project_id=incident.project.id, plugin_type="conference"
    )
    if not plugin:
        log.warning("Conference room not created. No conference plugin enabled.")
        return

    # Resolved off the ORM row now, while the session is certainly healthy.
    # `PluginInstance.instance` lazy-loads `self.plugin` to find the class, and
    # on a session a failed flush has left needing a rollback that read raises
    # -- into a handler that then dies on a `self.slug` the row does not have.
    # Reading it during compensation would lose the plugin in exactly the case
    # compensation exists for: a database failure. `incident.id` is read here
    # for the same reason, since an expired instance re-selects on access.
    conference_plugin = plugin.instance
    resource_type = plugin.plugin.slug
    plugin_title = plugin.plugin.title
    incident_id = incident.id

    # we create the external conference room
    try:
        external_conference = conference_plugin.create(
            incident.name, title=incident.title, participants=participants
        )
    except Exception as e:
        # A plugin whose own post-creation validation rejected a meeting the
        # provider had already accepted raises `ConferenceCreatedButUnusable`
        # and hands the id over on it -- the one failure in this span the flow
        # cannot see for itself, because the plugin never returns. Every other
        # exception means the provider gave us no resource to compensate for.
        if isinstance(e, ConferenceCreatedButUnusable):
            delete_unowned_conference(conference_plugin, e.resource_id, e)

        event_service.log_incident_event(
            db_session=db_session,
            source="Dispatch Core App",
            description=f"Creating the incident conference room failed. Reason: {e}",
            incident_id=incident.id,
        )
        log.exception(e)
        return

    if not external_conference:
        log.error(f"Conference not created. Plugin {resource_type} encountered an error.")
        return

    # Captured before anything can fail, and read defensively: a plugin free to
    # return an unusable mapping is equally free to return something that is not
    # one at all, and a subscript here would raise *outside* the guarded span --
    # losing the cleanup the only identifier it will ever have, silently.
    resource_id = external_conference.get("id") if isinstance(external_conference, dict) else None

    try:
        external_conference.update(
            {"resource_type": resource_type, "resource_id": external_conference["id"]}
        )

        # we create the internal conference room and attach it to the incident
        # in one transaction
        conference_in = ConferenceCreate(
            resource_id=external_conference["resource_id"],
            resource_type=external_conference["resource_type"],
            weblink=external_conference["weblink"],
            conference_id=external_conference["id"],
            conference_challenge=external_conference["challenge"],
        )
        conference = create(conference_in=conference_in, incident=incident, db_session=db_session)
    except Exception as e:
        # A raised COMMIT is not proof of a rolled-back COMMIT, so ownership is
        # confirmed before anything is destroyed.
        if not dispatch_owns_conference(db_session, incident_id, resource_id):
            delete_unowned_conference(conference_plugin, resource_id, e)
        # Re-raised deliberately. `incident_create_resources_flow` aborting is
        # the established behaviour for a Dispatch-side conference failure, and
        # `background_task` logs it with a traceback -- and rolls the session
        # back on the paths that let it open one. Returning None here would
        # report a created incident that quietly has no bridge.
        raise

    event_service.log_incident_event(
        db_session=db_session,
        source=plugin_title,
        description="Incident conference created",
        incident_id=incident.id,
    )

    return conference


def delete_conference(conference: Conference, project_id: int, db_session: SessionLocal):
    """Deletes an existing conference.

    Best effort, like every other external resource teardown: the incident is
    being deleted either way, so a provider that refuses is logged and dropped
    rather than allowed to wedge the delete flow. Nothing is written to the
    incident timeline -- the incident it belongs to is about to go with it,
    which leaves the log as the only record a leaked bridge ever gets. Hence
    the identifiers on it.

    `create_conference` writes the provider's meeting id into both
    `conference_id` and `resource_id`, so the two are equal and passing either
    works today. `conference_id` is the one the conference domain uses for the
    provider -- `update_conference_participant` already does -- and it is what
    every conference plugin's `delete` expects.

    Never log `weblink` or `conference_challenge`: the challenge is the meeting
    passcode, and a Zoom join_url commonly carries it in `?pwd=`.
    """
    if not conference:
        log.debug("Conference not deleted. No conference for this incident.")
        return

    if not conference.conference_id:
        # A row with no provider id: the bridge may exist provider-side and
        # this is the only chance to notice, so it is not a debug-level event.
        log.warning("Conference not deleted. Conference %s carries no provider id.", conference.id)
        return

    plugin = plugin_service.get_active_instance(
        db_session=db_session, project_id=project_id, plugin_type="conference"
    )
    if plugin:
        try:
            # every shipped conference plugin -- zoom, teams, google-calendar --
            # takes the provider's meeting id positionally, unlike ticket's
            # keyword `ticket_id`. `ConferencePlugin` declares `delete` as of
            # #114, so the signature is documented; it still raises
            # `NotImplementedError` rather than being abstract.
            plugin.instance.delete(conference.conference_id)
        except Exception as e:
            # `log.exception` alone records the reason but not the subject, and
            # Zoom's message names neither the meeting nor the project.
            log.exception(
                "Conference %s not deleted (project %s). Reason: %s",
                conference.conference_id,
                project_id,
                e,
            )
    else:
        log.warning("Conference not deleted. No conference plugin enabled.")
