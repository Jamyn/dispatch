"""Models for conference resources in the Dispatch application."""

from jinja2 import Template

from pydantic import field_validator, ValidationInfo
from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint

from dispatch.database.core import Base
from dispatch.messaging.strings import INCIDENT_CONFERENCE_DESCRIPTION
from dispatch.models import ResourceBase, ResourceMixin


class Conference(Base, ResourceMixin):
    """SQLAlchemy model for conference resources."""

    id = Column(Integer, primary_key=True)
    conference_id = Column(String)
    conference_challenge = Column(String, nullable=False, server_default="N/A")
    incident_id = Column(Integer, ForeignKey("incident.id", ondelete="CASCADE"))

    # One bridge per incident, in the database because the flow that creates it
    # is re-runnable and runs in more than one process: two runs can both pass
    # `if not incident.conference`, and `uselist=False` then leaves one of the
    # two provider meetings live and unreachable forever (issue #119). The
    # loser's INSERT now fails inside `create_conference`'s guarded span, which
    # deletes the meeting it just created.
    __table_args__ = (UniqueConstraint("incident_id", name="conference_incident_id_key"),)


# Pydantic models...
class ConferenceBase(ResourceBase):
    """Base Pydantic model for conference resources."""

    conference_id: str | None = None
    conference_challenge: str | None = None


class ConferenceCreate(ConferenceBase):
    """Pydantic model for creating a conference resource."""

    pass


class ConferenceUpdate(ConferenceBase):
    """Pydantic model for updating a conference resource."""

    pass


class ConferenceRead(ConferenceBase):
    """Pydantic model for reading a conference resource."""

    description: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def set_description(cls, v, info: ValidationInfo):
        """Sets the description using a Jinja2 template and the conference challenge."""
        conference_challenge = info.data.get("conference_challenge")
        return Template(INCIDENT_CONFERENCE_DESCRIPTION).render(
            conference_challenge=conference_challenge
        )
