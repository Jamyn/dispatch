from dispatch.plugins.bases import ConferencePlugin


class TestConferencePlugin(ConferencePlugin):
    title = "Dispatch Test Plugin - Conference"
    slug = "test-conference"

    def create(
        self, name: str, description: str = None, title: str = None, participants: list[str] = None
    ):
        return

    def delete(self, event_id: str):
        return

    def add_participant(self, event_id: str, participant: str):
        return

    def remove_participant(self, event_id: str, participant: str):
        return
