from dispatch.plugins.dispatch_atlassian_confluence import docs as confluence_doc_plugin
from dispatch.plugins.bases import DocumentPlugin
from dispatch.plugins.dispatch_atlassian_confluence.config import ConfluenceConfigurationBase
from dispatch.plugins.dispatch_atlassian_confluence.client import ConfluenceApi, confluence_api


def replace_content(api: ConfluenceApi, document_id: str, replacements: dict[str, str]) -> dict:
    # read content based on document_id
    current_content = api.get_page(document_id)
    current_content_body = current_content["body"]["storage"]["value"]
    for k, v in replacements.items():
        if v:
            current_content_body = current_content_body.replace(k, v)

    return api.update_page(page=current_content, body=current_content_body)


class ConfluencePageDocPlugin(DocumentPlugin):
    title = "Confluence pages plugin - Document Management"
    slug = "confluence-docs-document"
    description = "Use Confluence to update the contents."
    version = confluence_doc_plugin.__version__

    author = "Cino Jose"
    author_url = "https://github.com/Netflix/dispatch"

    def __init__(self):
        self.configuration_schema = ConfluenceConfigurationBase

    def update(self, document_id: str, **kwargs):
        """Replaces text in document."""
        kwargs = {"{{" + k + "}}": v for k, v in kwargs.items()}
        return replace_content(confluence_api(self.configuration), document_id, kwargs)
