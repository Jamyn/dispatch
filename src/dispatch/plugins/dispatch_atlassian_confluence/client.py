"""Confluence client construction, for atlassian-python-api 5.x.

5.x split Confluence into a Cloud client speaking REST v2 and a Server client
speaking REST v1, with different method signatures. The 4.x
``Confluence(cloud=...)`` factory routes any truthy ``cloud`` value to a class
carrying none of the methods used here, so ``hosting_type`` selects a class
rather than being passed through as a flag. Both call shapes live here so the
plugins never branch on the deployment type.

Every identifier crossing this boundary is a **page id**. Dispatch's storage
hierarchy is therefore pages under pages, and a page's space is read from its
parent rather than configured alongside it. Confluence Cloud does have folders
now, but a folder cannot be a page's space source here, so the root must be a
page.

Page titles are unique per space, not per parent, which is why creating a page
can rename it: see ``qualify_title``.
"""

from atlassian import ConfluenceServer, ConfluenceV2
from atlassian.confluence_base import ConfluenceBase

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
            url=self.site_url,
            username=configuration.username,
            password=configuration.password.get_secret_value(),
        )

    @property
    def site_url(self) -> str:
        """The instance root that both requests and weblinks hang off.

        AnyHttpUrl only appends a trailing slash when the path is empty, so a
        Server instance under a context path would otherwise be concatenated
        straight onto the next path segment.
        """
        return str(self.configuration.api_url).rstrip("/")

    def page_weblink(self, page: dict) -> str:
        """A browser URL for a page Confluence has just returned.

        ``_links.webui`` is site-relative and already carries the space key,
        which is otherwise a lookup. ``_links.base`` is deliberately ignored:
        on Server it is whatever the instance believes its own address to be,
        which need not be the one Dispatch was pointed at.

        Server's form names the page rather than identifying it, so a rename
        strands the stored link; the id form below is the fallback, not the
        default, because it is not the link Confluence's own UI hands out.
        """
        webui = (page.get("_links") or {}).get("webui")
        if webui:
            return f"{self.site_url}/{webui.lstrip('/')}"
        return f"{self.site_url}/pages/viewpage.action?pageId={page['id']}"

    def create_page(
        self, *, parent_id: str, title: str, body: str, qualify_title: bool = False
    ) -> dict:
        """Creates a page under ``parent_id``.

        ``qualify_title`` prefixes the parent's title. Confluence page titles
        are unique per space, so a name the caller reuses across subjects needs
        it; one that already names its subject does not.
        """
        raise NotImplementedError

    @staticmethod
    def _title_under(parent: dict, title: str, qualify: bool) -> str:
        return f"{parent['title']} - {title}" if qualify else title

    def _parent(self, page_id: str) -> dict:
        """The page a new page goes under, read for its space and its title."""
        raise NotImplementedError

    def get_page(self, page_id: str) -> dict:
        raise NotImplementedError

    def update_page(self, *, page: dict, body: str) -> dict:
        """Rewrites the body of a page already read with ``get_page``.

        Both clients need the page's current version to build an update, and
        one of them charges a request for it, so the caller hands over what it
        already has rather than naming the page again.
        """
        raise NotImplementedError


class CloudApi(ConfluenceApi):
    """Confluence Cloud, over the REST v2 API.

    Atlassian removed the v1 Cloud content endpoints on 2025-04-30, so this is
    the only API a Cloud deployment can be migrated onto.
    """

    client_class = ConfluenceV2

    @property
    def site_url(self) -> str:
        # Cloud always serves Confluence under /wiki, but the client appends
        # it only for hostnames it recognises, so a site on a custom domain
        # would address /api/v2 off the domain root. Gateway URLs already carry
        # /ex/confluence/{cloudId}; upstream's own test for them is reused
        # rather than restated, so the two cannot drift apart.
        url = super().site_url
        if url.endswith("/wiki") or ConfluenceBase._is_api_gateway_url(url):
            return url
        return f"{url}/wiki"

    def create_page(
        self, *, parent_id: str, title: str, body: str, qualify_title: bool = False
    ) -> dict:
        parent = self._parent(parent_id)
        return self.client.create_page(
            space_id=str(parent["spaceId"]),
            parent_id=parent_id,
            title=self._title_under(parent, title, qualify_title),
            body=body,
            body_format="storage",
            # Confluence answers 500, not 400, to a storage body with no
            # representation beside it. The client documents this argument as
            # wiki-only and omits it otherwise, so it has to be asked for.
            representation="storage",
        )

    def _parent(self, page_id: str) -> dict:
        # v2 requires spaceId on create even when parentId identifies the space
        # unambiguously. Asking for no body at all is not an option: the client
        # renders get_body=False as body-format=none, which the API rejects.
        return self.client.get_page_by_id(page_id)

    def get_page(self, page_id: str) -> dict:
        return self.client.get_page_by_id(page_id, body_format="storage")

    def update_page(self, *, page: dict, body: str) -> dict:
        # PageUpdateRequest requires id, status, title, body and version, and
        # the client sends `status` only when asked to. Supplying `version` is
        # not an optimisation: without it the client reads the page back with
        # get_body=False, which renders as body-format=none -- not a member of
        # PrimaryBodyRepresentationSingle, so the API rejects it.
        return self.client.update_page(
            page_id=page["id"],
            title=page["title"],
            body=body,
            body_format="storage",
            representation="storage",
            status="current",
            version=page["version"]["number"],
        )


class ServerApi(ConfluenceApi):
    """Confluence Server/Data Center, over the REST v1 API.

    The v1 signatures are unchanged from 4.0.7, including ``editor`` and
    ``full_width``, which have no v2 equivalent: v2 pages are always created in
    the new editor.
    """

    client_class = ConfluenceServer

    def create_page(
        self, *, parent_id: str, title: str, body: str, qualify_title: bool = False
    ) -> dict:
        parent = self._parent(parent_id)
        return self.client.create_page(
            space=parent["space"]["key"],
            parent_id=parent_id,
            title=self._title_under(parent, title, qualify_title),
            body=body,
            type="page",
            representation="storage",
            editor="v2",
            full_width=False,
        )

    def _parent(self, page_id: str) -> dict:
        # v1 addresses spaces by key, and a page carries its space only when
        # the expand is asked for.
        return self.client.get_page_by_id(page_id, expand="space")

    def get_page(self, page_id: str) -> dict:
        return self.client.get_page_by_id(page_id, expand="body.storage", status=None, version=None)

    def update_page(self, *, page: dict, body: str) -> dict:
        # v1 reads the version from the page's history itself.
        return self.client.update_page(
            page_id=page["id"],
            title=page["title"],
            body=body,
            representation="storage",
            type="page",
            parent_id=None,
            minor_edit=False,
            full_width=False,
        )


APIS = {HostingType.cloud: CloudApi, HostingType.server: ServerApi}


def confluence_api(configuration: ConfluenceConfigurationBase) -> ConfluenceApi:
    """Builds an authenticated API for the configured deployment."""
    return APIS[configuration.hosting_type](configuration)
