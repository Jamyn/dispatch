"""Moves the Confluence storage root onto the page id it now takes

The plugin used to hold two identifiers: `root_id`, a space key naming where
pages lived, and `parent_id`, the page each incident's own page was created
under. Both are page ids now and only `root_id` remains, so the value that
was already a page id moves across.

`parent_id` is removed rather than left in place. Plugin configuration
schemas ignore keys they do not declare, so a leftover would sit in the
stored JSON reading like live configuration until someone believed it.

A configuration with no `parent_id` is left alone, which makes this safe to
re-run and a no-op for an instance configured after the change.

Revision ID: b8d2f61c07ae
Revises: a7e4c9d18f52
Create Date: 2026-08-20

"""

import json

from alembic import op
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import Session, declarative_base, relationship
from sqlalchemy_utils import StringEncryptedType
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine

from dispatch.config import DISPATCH_ENCRYPTION_KEY

# revision identifiers, used by Alembic.
revision = "b8d2f61c07ae"
down_revision = "a7e4c9d18f52"
branch_labels = None
depends_on = None

Base = declarative_base()

CONFLUENCE_STORAGE_SLUG = "confluence"


class Plugin(Base):
    __tablename__ = "plugin"
    __table_args__ = {"schema": "dispatch_core"}
    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True)


class PluginInstance(Base):
    __tablename__ = "plugin_instance"
    id = Column(Integer, primary_key=True)
    _configuration = Column(
        StringEncryptedType(key=str(DISPATCH_ENCRYPTION_KEY), engine=AesEngine, padding="pkcs5")
    )
    plugin_id = Column(Integer, ForeignKey(Plugin.id))
    plugin = relationship(Plugin)


def migrated(configuration: dict) -> dict:
    """The stored configuration with the root moved onto the surviving field."""
    migrated_configuration = dict(configuration)
    parent_id = migrated_configuration.pop("parent_id", None)
    if parent_id:
        migrated_configuration["root_id"] = parent_id
    return migrated_configuration


def upgrade():
    session = Session(bind=op.get_bind())

    instances = (
        session.query(PluginInstance)
        .join(Plugin)
        .filter(Plugin.slug == CONFLUENCE_STORAGE_SLUG)
        .all()
    )

    for instance in instances:
        if not instance._configuration:
            continue
        instance._configuration = json.dumps(migrated(json.loads(instance._configuration)))

    session.commit()


def downgrade():
    # The space key this replaced is not recoverable from what is kept.
    pass
