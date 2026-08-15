try:
    from pydantic.v1 import PydanticValueError
except ImportError:
    from pydantic import PydanticValueError


class DispatchException(Exception):
    pass


class DispatchPluginException(DispatchException):
    pass


class ConferenceCreatedButUnusable(DispatchPluginException):
    """The provider accepted a meeting the plugin cannot hand back (issue #114).

    Raised only from a conference plugin's own post-creation validation, where
    the provider has already committed a meeting and the plugin has then found
    the response unusable. ``resource_id`` carries the provider's id when the
    response supplied one, so ``create_conference`` can delete the bridge it
    will never own; it is None when the provider omitted the id, which leaves
    nothing safe to delete by.

    A plugin that never reached the provider must raise ``DispatchPluginException``
    instead. The two are distinguished here rather than by inspecting messages
    because the difference decides whether an external resource gets deleted.
    """

    def __init__(self, message: str, resource_id=None):
        super().__init__(message)
        self.resource_id = resource_id


class ConferenceRosterUnreadable(DispatchPluginException):
    """The provider would not report the roster, so it was left alone (issue #129).

    Distinguished from an ordinary plugin failure because it is not one: nothing
    was attempted and nothing broke. The roster is only ever written wholesale,
    so a provider that will not report the current list leaves no safe way to
    change one entry of it, and declining is the correct outcome rather than a
    degraded one.

    It exists so `update_conference_participant` can tell the two apart. A real
    failure belongs on the incident timeline; this does not, and putting it
    there would be actively wrong -- incident creation seeds the bridge and then
    walks the very same responders through the add flow, so every founding
    responder would get a timeline entry saying they could not be added to a
    conference they are already listed on.

    Not the same as `ConferenceAlreadyGone`, which pulls the other way: there a
    provider 404 on teardown means the desired end state was reached. Here it
    was not -- the roster really is unchanged. The two are distinguished by
    whether the intent was satisfied, never by whether the provider was happy,
    which is why issue #120 got its own exception rather than a generalisation
    of this one.
    """


class ConferenceAlreadyGone(DispatchPluginException):
    """The provider has no such meeting, so teardown's intent is already met (issue #120).

    Raised only from a *delete*, and only for the provider's own not-found
    answer. Teardown wants the bridge gone; a provider that says it has no such
    meeting has delivered exactly that, and reporting it as a failed deletion
    tells an operator a live meeting leaked when none did.

    Scoped to deletes at the point where each transport still knows the method,
    deliberately. The same status means something else on every other call --
    Zoom answers 404 to a create naming an `api_user_id` it cannot resolve, and
    a 404 on the read half of a roster update deleted nothing -- so this is
    never derived from a status alone further up.

    A `DispatchPluginException` subclass so that a caller which does not opt in
    keeps its existing behaviour exactly. Compare `ConferenceRosterUnreadable`,
    which is the mirror image: there the provider declined to answer and the
    intent was *not* met. Both are decided by whether the intent was satisfied,
    never by whether the provider was happy.
    """


class NotFoundError(PydanticValueError):
    code = "not_found"
    msg_template = "{msg}"


class FieldNotFoundError(PydanticValueError):
    code = "not_found.field"
    msg_template = "{msg}"


class ModelNotFoundError(PydanticValueError):
    code = "not_found.model"
    msg_template = "{msg}"


class ExistsError(PydanticValueError):
    code = "exists"
    msg_template = "{msg}"


class InvalidConfigurationError(PydanticValueError):
    code = "invalid.configuration"
    msg_template = "{msg}"


class InvalidFilterError(PydanticValueError):
    code = "invalid.filter"
    msg_template = "{msg}"


class InvalidUsernameError(PydanticValueError):
    code = "invalid.username"
    msg_template = "{msg}"


class InvalidPasswordError(PydanticValueError):
    code = "invalid.password"
    msg_template = "{msg}"
