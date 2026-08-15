from typing import TYPE_CHECKING

from dispatch.exceptions import DispatchException

from .models import Conference, ConferenceCreate

if TYPE_CHECKING:
    # Imported for the annotation only. `dispatch.incident.models` pulls in most
    # of the incident graph, and this is a leaf module the conference flow and
    # the plugins both sit above.
    from dispatch.incident.models import Incident


def get(*, db_session, conference_id: int) -> Conference | None:
    """Get a conference by its id."""
    return db_session.query(Conference).filter(Conference.id == conference_id).one()


def get_by_resource_id(*, db_session, resource_id: str) -> Conference | None:
    """Get a conference by its id."""
    return db_session.query(Conference).filter(Conference.resource_id == resource_id).one_or_none()


def get_by_incident_id(*, db_session, incident_id: str) -> Conference | None:
    """Get a conference by its associated incident id."""
    return db_session.query(Conference).filter(Conference.incident_id == incident_id).one()


def get_all(*, db_session):
    """Get all conferences."""
    return db_session.query(Conference)


def create(
    *, db_session, conference_in: ConferenceCreate, incident: "Incident | None" = None
) -> Conference:
    """Create a new conference, owned from the first commit by `incident`.

    Passing `incident` is what makes the row and the link that makes it findable
    a single transaction. A `Conference` committed with a NULL `incident_id` is
    already lost: `incident_delete_flow` reaches a bridge only through
    `incident.conference`, so a failure between two commits used to strand the
    row *and* the provider's meeting behind it (issue #114).

    `incident` stays optional because the association is not this function's
    business for every caller, but the conference flow always passes it -- the
    single-commit guarantee is only as good as that argument.

    Refuses an incident that already has a conference. `Incident.conference`
    cascades `delete-orphan`, so assigning over one silently deletes the old row
    while its provider meeting stays live -- trading a database orphan for
    exactly the permanent provider orphan #114 exists to prevent, with no
    exception raised anywhere. Raising instead puts the caller inside
    `create_conference`'s guarded span, where the meeting it just created is
    compensated away and the existing bridge survives untouched.
    """
    conference = Conference(**conference_in.dict())
    if incident is not None:
        if incident.conference is not None:
            raise DispatchException(
                f"Incident {incident.id} already has a conference. Refusing to replace it, "
                "which would delete the existing row and strand its provider meeting."
            )
        incident.conference = conference
        db_session.add(incident)
    db_session.add(conference)
    db_session.commit()
    return conference
