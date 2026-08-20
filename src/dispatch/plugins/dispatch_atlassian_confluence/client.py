"""Confluence client construction, for atlassian-python-api 5.x.

5.x split Confluence into a Cloud client speaking REST v2 and a Server client
speaking REST v1, with different method signatures. The 4.x
``Confluence(cloud=...)`` factory routes any truthy ``cloud`` value to a class
carrying none of the methods used here, so ``hosting_type`` selects a class
rather than being passed through as a flag. Both call shapes live here so the
plugins never branch on the deployment type.
"""

from atlassian import ConfluenceServer, ConfluenceV2

from dispatch.plugins.dispatch_atlassian_confluence.config import (
    ConfluenceConfigurationBase,
    HostingType,
)


class ConfluenceApi:
    """The Confluence operations Dispatch performs, over one 5.x client."""

    client_class = None

    def __init__(self, configuration: ConfluenceConfigurationBase):
        self.configuration = configuration
        self.client = self.client_class(
            url=str(configuration.api_url),
            username=configuration.username,
            password=configuration.password.get_secret_value(),
        )


class CloudApi(ConfluenceApi):
    """Confluence Cloud, over the REST v2 API.

    Atlassian removed the v1 Cloud content endpoints on 2025-04-30, so this is
    the only API a Cloud deployment can be migrated onto.
    """

    client_class = ConfluenceV2

    def create_page(self, *, space: str, title: str, body: str, parent_id: str) -> dict:
        # v2 addresses spaces by numeric id. `space` is the configured space
        # key, which the v2 page endpoint rejects, so resolve it first.
        space_id = self.client.get_space_by_key(space)["id"]
        return self.client.create_page(
            space_id=str(space_id),
            title=title,
            body=body,
            parent_id=parent_id,
            body_format="storage",
        )

    def get_page(self, page_id: str) -> dict:
        return self.client.get_page_by_id(page_id, body_format="storage")

    def update_page(self, *, page_id: str, title: str, body: str) -> dict:
        return self.client.update_page(
            page_id=page_id, title=title, body=body, body_format="storage"
        )

    def page_weblink(self, *, space: str, page_id: str, title: str) -> str:
        return f"{self.configuration.api_url}wiki/spaces/{space}/pages/{page_id}/{title}"


class ServerApi(ConfluenceApi):
    """Confluence Server/Data Center, over the REST v1 API.

    The v1 signatures are unchanged from 4.0.7, including ``editor`` and
    ``full_width``, which have no v2 equivalent: v2 pages are always created in
    the new editor.
    """

    client_class = ConfluenceServer

    def create_page(self, *, space: str, title: str, body: str, parent_id: str) -> dict:
        return self.client.create_page(
            space=space,
            title=title,
            body=body,
            parent_id=parent_id,
            type="page",
            representation="storage",
            editor="v2",
            full_width=False,
        )

    def get_page(self, page_id: str) -> dict:
        return self.client.get_page_by_id(page_id, expand="body.storage", status=None, version=None)

    def update_page(self, *, page_id: str, title: str, body: str) -> dict:
        return self.client.update_page(
            page_id=page_id,
            title=title,
            body=body,
            representation="storage",
            type="page",
            parent_id=None,
            minor_edit=False,
            full_width=False,
        )

    def page_weblink(self, *, space: str, page_id: str, title: str) -> str:
        # Server has no /wiki context path, and addressing by page id avoids
        # having to encode the space key and title into the path.
        return f"{self.configuration.api_url}pages/viewpage.action?pageId={page_id}"


APIS = {HostingType.cloud: CloudApi, HostingType.server: ServerApi}


def confluence_api(configuration: ConfluenceConfigurationBase) -> ConfluenceApi:
    """Builds an authenticated API for the configured deployment."""
    return APIS[configuration.hosting_type](configuration)
