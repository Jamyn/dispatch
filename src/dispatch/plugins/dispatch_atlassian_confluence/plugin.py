from dispatch.plugins import dispatch_atlassian_confluence as confluence_plugin
from dispatch.plugins.bases import StoragePlugin
from dispatch.plugins.dispatch_atlassian_confluence.client import confluence_api
from dispatch.plugins.dispatch_atlassian_confluence.config import ConfluenceConfigurationBase

from pydantic import Field

import requests
from requests.auth import HTTPBasicAuth
import logging

logger = logging.getLogger(__name__)


def describe(exception: Exception) -> str:
    """Renders an exception that may carry nothing but a response.

    The Cloud client's raise_for_status does not understand v2's error
    envelope, so HTTPError arrives with an empty message and the status is the
    only thing left to report. The body is deliberately not included: it is not
    audited for what a Confluence error may quote back.
    """
    response = getattr(exception, "response", None)
    if str(exception) or response is None:
        return str(exception)
    return f"HTTP {response.status_code} {response.reason}"


# TODO : Use the common config from the root directory.
class ConfluenceConfiguration(ConfluenceConfigurationBase):
    """Confluence configuration description."""

    template_id: str = Field(
        title="Incident template ID", description="This is the page id of the template."
    )
    root_id: str = Field(
        title="Default Space ID", description="Defines the default Confluence Space to use."
    )
    parent_id: str = Field(
        title="Parent ID for the pages",
        description="Define the page id of a parent page where all the incident documents can be kept.",
    )
    open_on_close: bool = Field(
        title="Open On Close",
        default=False,
        description="Controls the visibility of resources on incident close. If enabled Dispatch will make all resources visible to the entire workspace.",
    )
    read_only: bool = Field(
        title="Readonly",
        default=False,
        description="The incident document will be marked as readonly on incident close. Participants will still be able to interact with the document but any other viewers will not.",
    )


class ConfluencePagePlugin(StoragePlugin):
    title = "Confluence Plugin - Store your incident details"
    slug = "confluence"
    description = "Confluence plugin to create incident documents"
    version = confluence_plugin.__version__

    author = "Cino Jose"
    author_url = "https://github.com/Netflix/dispatch"

    def __init__(self):
        self.configuration_schema = ConfluenceConfiguration

    def create_file(
        self, parent_id: str, name: str, participants: list[str] = None, file_type: str = "folder"
    ):
        """Creates a new Home page for the incident documents.."""
        try:
            if file_type not in ["document", "folder"]:
                return None
            # The storage interface is Drive-shaped: callers pass the project's
            # storage root, which for Confluence is a space key. The ancestor
            # page comes from the plugin configuration instead.
            space = parent_id
            api = confluence_api(self.configuration)
            child_display_body = """<h3>Incident Documents:</h3><ac:structured-macro ac:name="children"
                                    ac:schema-version="2" data-layout="default" ac:local-id="ec0e8d6d-3215-4328-b1f8-e96b03ccefb9"
                                    ac:macro-id="10235d28b48543519d4e2b06ca230142"><ac:parameter ac:name="sort">modified</ac:parameter>
                                    <ac:parameter ac:name="reverse">true</ac:parameter></ac:structured-macro>"""
            page_details = api.create_page(
                space=space,
                title=name,
                body=child_display_body,
                parent_id=self.configuration.parent_id,
            )
            return {
                "weblink": api.page_weblink(space=space, page_id=page_details["id"], title=name),
                "id": page_details["id"],
                "name": name,
                "description": "",
            }
        except Exception as e:
            logger.error(f"Exception happened while creating page: {describe(e)}")

    def copy_file(self, folder_id: str, file_id: str, name: str):
        # TODO : This is the function that is responsible for making the incident documents.
        try:
            api = confluence_api(self.configuration)
            logger.info(f"Copy_file function with args {folder_id}, {file_id}, {name}")
            template_content = api.get_page(self.configuration.template_id)
            page_details = api.create_page(
                space=self.configuration.root_id,
                parent_id=folder_id,
                title=name,
                body=template_content["body"]["storage"]["value"],
            )
            if self.configuration.parent_id:
                """TODO: Find and fix why the page is not created under the parent_id, folder_id"""
                self.move_file_confluence(page_id_to_move=page_details["id"], parent_id=folder_id)
            return {
                "weblink": api.page_weblink(
                    space=self.configuration.root_id, page_id=page_details["id"], title=name
                ),
                "id": page_details["id"],
                "name": name,
            }
        except Exception as e:
            logger.error(f"Exception happened while creating page: {describe(e)}")

    def move_file(self, new_folder_id: str, file_id: str, **kwargs):
        """Moves a file from one place to another. Not used in the plugin,
        keeping the body as the interface is needed to avoid exceptions."""
        return {}

    def move_file_confluence(self, page_id_to_move: str, parent_id: str):
        try:
            url = f"{self.configuration.api_url}wiki/rest/api/content/{page_id_to_move}/move/append/{parent_id}"
            auth = HTTPBasicAuth(
                self.configuration.username, self.configuration.password.get_secret_value()
            )
            headers = {"Accept": "application/json"}
            response = requests.request("PUT", url, headers=headers, auth=auth)
            return response
        except Exception as e:
            logger.error(f"Exception happened while moving page: {describe(e)}")
