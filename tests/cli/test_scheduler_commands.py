"""Listing the scheduled tasks.

`dispatch scheduler list` is how an operator sees what the scheduler will run
and when. It read `Job.period`, which the `schedule` library does not expose --
so it raised for every invocation, whatever was registered.
"""

import pytest
from click.testing import CliRunner

from dispatch.cli import dispatch_cli


@pytest.fixture
def runner():
    return CliRunner()


def test_listing_the_scheduled_tasks_names_them_and_their_period(runner):
    """Given registered tasks, when they are listed, then each is shown with its period.

    Asserting on a real registered task rather than just the exit code: the
    command failed on an attribute of the job, so a table that renders but omits
    the jobs would be the same bug wearing a hat.
    """
    result = runner.invoke(dispatch_cli, ["scheduler", "list"])

    assert result.exit_code == 0, repr(result.exception)
    assert "sync-tags" in result.output, "a registered task was not listed"
    assert "hours" in result.output, "the period was not rendered"
