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
