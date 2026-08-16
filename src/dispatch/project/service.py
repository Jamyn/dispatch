from pydantic import ValidationError
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import true


from .models import Project, ProjectCreate, ProjectRead, ProjectUpdate

# Sorts a project under the name it is listed by: its display name, falling
# back to its name. The 2024-12-12 migration seeded display_name from name and
# left the column defaulting to '', so a project created since without one
# would otherwise sort under nothing.
PROJECT_LABEL = func.coalesce(func.nullif(Project.display_name, ""), Project.name)

# Escaped so a display name containing % or _ matches literally rather than
# turning into a wildcard.
_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    """`value` with every LIKE metacharacter made literal."""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def _relevance(query_str: str | None) -> tuple:
    """Ordering terms that put the best matches first, or nothing for no query.

    Compared against the whole query string rather than the individual words it
    was matched on: "exact" and "prefix" are properties of what the user typed,
    and a multi-word query that is an exact label is still an exact label.
    """
    if not query_str or not query_str.strip():
        return ()

    lowered = query_str.strip().lower()
    label = func.lower(PROJECT_LABEL)
    return (
        case(
            (label == lowered, 0),
            (label.startswith(_escape_like(lowered), escape=_LIKE_ESCAPE), 1),
            else_=2,
        ),
    )


def get(*, db_session: Session, project_id: int) -> Project | None:
    """Returns a project based on the given project id."""
    return db_session.query(Project).filter(Project.id == project_id).first()


def get_default(*, db_session: Session) -> Project | None:
    """Returns the default project."""
    return db_session.query(Project).filter(Project.default == true()).one_or_none()


def get_default_or_raise(*, db_session: Session) -> Project:
    """Returns the default project or raise a ValidationError if one doesn't exist."""
    project = get_default(db_session=db_session)

    if not project:
        raise ValidationError(
            [
                {
                    "loc": ("project",),
                    "msg": "No default project defined.",
                    "type": "value_error",
                }
            ]
        )
    return project


def get_by_name(*, db_session: Session, name: str) -> Project | None:
    """Returns a project based on the given project name."""
    return db_session.query(Project).filter(Project.name == name).one_or_none()


def get_by_name_or_raise(*, db_session: Session, project_in: ProjectRead) -> Project:
    """Returns the project specified or raises ValidationError."""
    project = get_by_name(db_session=db_session, name=project_in.name)

    if not project:
        raise ValidationError(
            [
                {
                    "msg": "Project not found.",
                    "name": project_in.name,
                    "loc": "name",
                }
            ]
        )

    return project


def get_by_name_or_default(*, db_session, project_in: ProjectRead) -> Project:
    """Returns a project based on a name or the default if not specified."""
    if project_in and project_in.name:
        project = get_by_name(db_session=db_session, name=project_in.name)
        if project:
            return project
    return get_default_or_raise(db_session=db_session)


def get_all(*, db_session) -> list[Project | None]:
    """Returns all projects."""
    return db_session.query(Project)


def get_all_enabled(
    *, db_session: Session, query_str: str | None = None, limit: int | None = None
) -> list[Project]:
    """Returns enabled projects, ordered by label and narrowed by a substring match.

    Filtering, ordering and limiting all happen in the database so a caller
    that only wants a page of options does not pay for the whole table.
    Ordering is case-insensitive by label with the id as a tiebreaker, so the
    same call always returns the same rows in the same order.

    Deliberately a substring match rather than the full-text `search_vector`
    the tag lookup uses: that vector covers `name` and `description` but not
    `display_name`, which is the string a selector actually shows and a user
    actually types. Full-text search also matches whole word prefixes, so
    "ments" would never find "Payments".

    Both `display_name` and `name` are matched even though only the label is
    displayed, because operators know projects under both.

    A query of several words matches in any order: each word is its own
    `ILIKE`, ANDed. One pattern over the whole string would find "Security
    Engineering" for `security eng` but nothing at all for "Security Platform
    Engineering", which is the same project to the person typing.

    Results are tiered by match quality before the alphabetical sort -- exact
    label, then label prefix, then anything else. Without that, `sec` against
    200 projects named `aaa-sec-NNN` fills the whole limit alphabetically and
    the project actually called `Security` is not in the response at all, even
    though the user typed an exact prefix of it (#146).
    """
    query = db_session.query(Project).filter(Project.enabled == true())

    words = query_str.split() if query_str else []
    for word in words:
        pattern = f"%{_escape_like(word)}%"
        query = query.filter(
            or_(
                Project.display_name.ilike(pattern, escape=_LIKE_ESCAPE),
                Project.name.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        )

    query = query.order_by(*_relevance(query_str), func.lower(PROJECT_LABEL), Project.id)

    if limit is not None:
        query = query.limit(limit)

    return query.all()


def create(*, db_session, project_in: ProjectCreate) -> Project:
    """Creates a project."""
    from dispatch.organization import service as organization_service

    organization = organization_service.get_by_slug(
        db_session=db_session, slug=project_in.organization.slug
    )
    project = Project(
        **project_in.dict(exclude={"organization"}),
        organization_id=organization.id,
    )

    db_session.add(project)
    db_session.commit()
    return project


def get_or_create(*, db_session, project_in: ProjectCreate) -> Project:
    if project_in.id:
        q = db_session.query(Project).filter(Project.id == project_in.id)
    else:
        q = db_session.query(Project).filter_by(**project_in.dict(exclude={"id", "organization"}))

    instance = q.first()
    if instance:
        return instance

    return create(db_session=db_session, project_in=project_in)


def update(*, db_session, project: Project, project_in: ProjectUpdate) -> Project:
    """Updates a project."""
    project_data = project.dict()

    update_data = project_in.dict(exclude_unset=True, exclude={})

    for field in project_data:
        if field in update_data:
            setattr(project, field, update_data[field])

    db_session.commit()
    return project


def delete(*, db_session, project_id: int):
    """Deletes a project."""
    project = db_session.query(Project).filter(Project.id == project_id).first()
    db_session.delete(project)
    db_session.commit()
