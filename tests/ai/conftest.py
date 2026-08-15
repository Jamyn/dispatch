"""Fixtures shared by the ai tests.

`test_ai_service_contract.py` and `test_ai_service_contract_anthropic.py` drive
the ai service through the *real* OpenAI and Anthropic plugins, so they need the
same in-memory endpoints the plugins' own tests use. Re-exported here rather
than duplicated, so the suites can never disagree about what the fakes answer.
"""

from tests.plugins.dispatch_anthropic.conftest import (  # noqa: F401
    anthropic_api,
    anthropic_plugin,
)
from tests.plugins.dispatch_openai.conftest import (  # noqa: F401
    openai_api,
    openai_plugin,
)
