from dispatch.plugins import dispatch_atlassian_confluence as confluence_plugin
from dispatch.plugins.bases import StoragePlugin
from dispatch.plugins.dispatch_atlassian_confluence.client import confluence_api
from dispatch.plugins.dispatch_atlassian_confluence.config import ConfluenceConfigurationBase

from pydantic import Field

import logging

logger = logging.getLogger(__name__)

# A folder stands in for a directory listing, so its body is the index of what
# was filed under it. A document's body is its own content.
CHILD_INDEX_BODY = """<h3>Incident Documents:</h3><ac:structured-macro ac:name="children"
                      ac:schema-version="2" data-layout="default" ac:local-id="ec0e8d6d-3215-4328-b1f8-e96b03ccefb9"
                      ac:macro-id="10235d28b48543519d4e2b06ca230142"><ac:parameter ac:name="sort">modified</ac:parameter>
                      <ac:parameter ac:name="reverse">true</ac:parameter></ac:structured-macro>"""


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

    root_id: str = Field(
        title="Root Incident Storage Page ID",
        description=(
            "Page id of the Confluence page each incident's page is created under. "
            "A page id, not a space key: Confluence has no folders, so Dispatch's "
            "storage hierarchy is pages beneath pages."
        ),
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
        """Creates a page beneath the page it is given.

        Core calls this for the subject's own page, passing the project storage
        root, and again for each sub-folder, passing the page it just got back.
        Confluence has no folder a document can live in, so both are pages.
        """
        try:
            if file_type not in ["document", "folder"]:
                return None
            api = confluence_api(self.configuration)
            # A folder's name is a project setting, so every subject reuses it,
            # and Confluence titles are unique per space. A document already
            # carries its subject's name; the subject's page sits on the root.
            qualify_title = file_type == "folder" and parent_id != self.configuration.root_id
            page_details = api.create_page(
                parent_id=parent_id,
                title=name,
                body=CHILD_INDEX_BODY if file_type == "folder" else "",
                qualify_title=qualify_title,
            )
            return {
                "weblink": api.page_weblink(page_details),
                "id": page_details["id"],
                "name": name,
                "description": "",
            }
        except Exception as e:
            logger.error(
                f"Exception happened while creating page {name!r} under {parent_id}: {describe(e)}"
            )

    def copy_file(self, folder_id: str, file_id: str, name: str):
        """Copies the page it is given into the page it is given.

        `file_id` is the template Dispatch resolved for this document -- the
        incident type's own template, the executive report's, or the form
        export's. They are different pages, and which one this is is the
        caller's decision to make.
        """
        try:
            api = confluence_api(self.configuration)
            template_content = api.get_page(file_id)
            page_details = api.create_page(
                parent_id=folder_id,
                title=name,
                body=template_content["body"]["storage"]["value"],
            )
            return {
                "weblink": api.page_weblink(page_details),
                "id": page_details["id"],
                "name": name,
            }
        except Exception as e:
            logger.error(f"Exception happened while creating page {name!r}: {describe(e)}")

    def delete_file(self, file_id: str, **kwargs):
        """Removes a page and everything filed beneath it.

        Failures are left to `delete_storage`, which logs them: swallowing one
        here would report storage as cleaned up when it is still there.
        """
        confluence_api(self.configuration).delete_page(file_id)

    def move_file(self, new_folder_id: str, file_id: str, **kwargs):
        """Moves a file from one place to another. Not used in the plugin,
        keeping the body as the interface is needed to avoid exceptions.

        create_page files a page under its parent, so there is nothing left for
        the callers that follow copy_file with a move.
        """
        return {}
