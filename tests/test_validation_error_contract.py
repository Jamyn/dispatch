"""Every ValidationError raised in the tree must be one a caller can render.

`ExceptionMiddleware` turns a pydantic ValidationError into a 422 carrying
`.errors()`; anything else reaches the caller as an opaque 500. Two ways of
building one raise `TypeError` at the point of raise instead, so the guard never
fires and the message is lost:

* direct construction, which pydantic v2 removed, and
* `from_exception_data` with a `value_error` line error that carries no `ctx`.

Both were live across this tree, neither is visible to the linter, and neither
shows up until the guard is actually hit -- which is the moment it is needed.
"""

import ast
import pathlib

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "dispatch"


def _modules():
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        try:
            yield path, ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a parse failure is python-lint's job
            continue


def _line_error_dicts(call: ast.Call):
    """The line-error dict literals passed to a from_exception_data call."""
    for arg in call.args:
        if isinstance(arg, ast.List):
            for element in arg.elts:
                if isinstance(element, ast.Dict):
                    yield element


def _keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant)}


def _where(path: pathlib.Path, node: ast.AST) -> str:
    return f"{path.relative_to(SOURCE_ROOT.parents[1])}:{node.lineno}"


def test_no_validation_error_is_built_by_direct_construction():
    """Given the tree, when it raises a ValidationError, then it never constructs one directly.

    `ValidationError([...])` and `ValidationError([...], model=X)` are both
    pydantic v1 spellings; v2 raises TypeError for either.
    """
    offenders = [
        _where(path, node)
        for path, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ValidationError"
    ]

    assert not offenders, (
        "use ValidationError.from_exception_data(...) instead; direct construction "
        "raises TypeError on pydantic v2:\n  " + "\n  ".join(offenders)
    )


def test_every_value_error_carries_the_context_pydantic_requires():
    """Given a value_error line error, when it is built, then it carries `ctx`.

    A `msg` key alone does not satisfy pydantic: it wants the underlying error
    in `ctx`, and raises `TypeError: ValueError: 'error' required in context`
    without it. Sites that had already moved to `from_exception_data` were still
    failing this way.
    """
    offenders = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_exception_data"
            ):
                continue
            for line_error in _line_error_dicts(node):
                keys = _keys(line_error)
                declared = {
                    k.value: v
                    for k, v in zip(line_error.keys, line_error.values, strict=True)
                    if isinstance(k, ast.Constant)
                }
                error_type = declared.get("type")
                is_value_error = (
                    isinstance(error_type, ast.Constant) and error_type.value == "value_error"
                )
                if is_value_error and "ctx" not in keys:
                    offenders.append(_where(path, node))

    assert not offenders, (
        'a "value_error" line error needs {"ctx": {"error": ValueError(...)}} or pydantic '
        "raises TypeError:\n  " + "\n  ".join(offenders)
    )


def test_every_reported_input_is_a_field_the_model_actually_has():
    """Given a line error naming an input, when it is built, then that attribute exists.

    These guards are the untested half of the tree -- they only run when a
    lookup misses -- so a wrong attribute here surfaces as an AttributeError in
    front of a user rather than in CI. `service.py` shipped exactly that bug,
    reading `external_id` off the `None` it had just tested for.
    """
    import importlib

    offenders = []
    for path, tree in _modules():
        text = path.read_text()
        functions = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_exception_data"
            ):
                continue
            for line_error in _line_error_dicts(node):
                declared = {
                    k.value: v
                    for k, v in zip(line_error.keys, line_error.values, strict=True)
                    if isinstance(k, ast.Constant)
                }
                reported = declared.get("input")
                if not (
                    isinstance(reported, ast.Attribute) and isinstance(reported.value, ast.Name)
                ):
                    continue  # a literal or a call -- nothing to resolve
                parameter, attribute = reported.value.id, reported.attr

                enclosing = max(
                    (f for f in functions if f.lineno <= node.lineno <= f.end_lineno),
                    key=lambda f: f.lineno,
                    default=None,
                )
                if enclosing is None:
                    continue
                annotation = next(
                    (
                        ast.get_source_segment(text, a.annotation)
                        for a in enclosing.args.args + enclosing.args.kwonlyargs
                        if a.arg == parameter and a.annotation is not None
                    ),
                    None,
                )
                if annotation is None:
                    continue  # unannotated, or an ORM object rather than a schema

                module_name = ".".join(path.relative_to(SOURCE_ROOT.parent).with_suffix("").parts)
                model = getattr(
                    importlib.import_module(module_name), annotation.split("[")[0].strip(), None
                )
                fields = getattr(model, "model_fields", None)
                if fields is None:
                    continue  # not a pydantic model
                if attribute not in fields:
                    offenders.append(
                        f"{_where(path, node)}  {parameter}.{attribute} "
                        f"is not a field of {annotation}"
                    )

    assert not offenders, "these raise an AttributeError instead of the error they build:\n  " + (
        "\n  ".join(offenders)
    )
