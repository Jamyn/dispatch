"""`dispatch server slack` must keep resolving what it imports.

`run_slack_websocket` does all of its importing inside the function body, so
nothing in the suite ever executes those statements -- the command can be dead
on arrival while every test passes. That is not hypothetical: moving the Bolt
app off a module global broke it, and the whole suite stayed green.

This walks the function's own import statements and resolves each one, which
costs nothing and fails the moment the CLI drifts from the modules it uses.
"""

import ast
import importlib
import inspect
import re
import textwrap


def command_source(command) -> str:
    """The body of a click command, unwrapped from its decorator."""
    return textwrap.dedent(inspect.getsource(getattr(command, "callback", command)))


def local_imports(command) -> list[tuple[str, str]]:
    """Every `from X import Y` written inside the command, as (module, name)."""
    tree = ast.parse(command_source(command))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.extend((node.module, alias.name) for alias in node.names)
    return found


def test_socket_mode_imports_resolve():
    from dispatch.cli import run_slack_websocket

    imports = local_imports(run_slack_websocket)
    assert imports, "expected function-local imports; has run_slack_websocket been rewritten?"

    unresolved = []
    for module_name, attribute in imports:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            unresolved.append(f"{module_name}: {exc}")
            continue
        if not hasattr(module, attribute):
            unresolved.append(f"{module_name}.{attribute} does not exist")

    assert not unresolved, "dispatch server slack would fail on startup: " + "; ".join(unresolved)


def test_socket_mode_builds_its_app_through_the_shared_factory():
    """Socket mode must not hand-roll its own app.

    It used to configure a shared app itself and assign its token onto it,
    which both mutated process-global state and quietly omitted one of the
    configure functions -- so socket mode ran with a different listener set
    than the HTTP routes.
    """
    from dispatch.cli import run_slack_websocket

    source = command_source(run_slack_websocket)

    assert ("dispatch.plugins.dispatch_slack.app", "build_app") in local_imports(
        run_slack_websocket
    )

    # An assignment onto a Bolt app's private tenant state -- `app._token = ...`
    # is what this whole change removes. Matches the assignment, not the word,
    # so `socket_mode_app_token` does not trip it.
    assignments = re.findall(r"\w+\._(?:token|configuration|signing_secret)\s*=(?!=)", source)
    assert not assignments, f"socket mode is mutating app state again: {assignments}"
