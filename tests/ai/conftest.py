"""Fixtures shared by the ai tests.

`test_ai_service_contract.py` drives the ai service through the *real* OpenAI
plugin, so it needs the same in-memory OpenAI endpoint the plugin's own tests
use. Re-exported here rather than duplicated, so the two suites can never
disagree about what the fake answers.
"""

from tests.plugins.dispatch_openai.conftest import (  # noqa: F401
    openai_api,
    openai_plugin,
)
