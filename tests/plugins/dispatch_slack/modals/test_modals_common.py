from unittest.mock import MagicMock

from dispatch.plugins.dispatch_slack.modals.common import send_success_modal


class TestSendSuccessModal:
    def test_sends_the_requested_modal(self):
        client = MagicMock()

        send_success_modal(client=client, view_id="view_id", title="All done", message="It worked.")

        view = client.views_update.call_args.kwargs["view"]
        assert view["title"]["text"] == "All done"

    def test_falls_back_to_default_modal_when_blockkit_rejects_the_input(self):
        """A too-long title makes blockkit reject the modal at build() time.

        Guards the except clause: blockkit 2 raises its own errors rather than
        pydantic's, so a stale exception type would let this propagate instead
        of falling back.
        """
        client = MagicMock()

        send_success_modal(client=client, view_id="view_id", title="x" * 200, message="It worked.")

        view = client.views_update.call_args.kwargs["view"]
        assert view["title"]["text"] == "Done"
        assert view["blocks"][0]["text"]["text"] == "Success!"
