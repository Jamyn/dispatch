"""Every TypeScript file outside the frontend package must be in the root tsconfig.

The Playwright tree is a second npm package (`dispatch-e2e`) whose sources sit
outside `src/dispatch/static/dispatch/tsconfig.json`. Playwright transpiles them
without consulting any tsconfig and never type-checks, so a `.ts` file that
falls out of the root `include` is silently unchecked rather than loudly broken.
"""

import json
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TSCONFIG = REPO_ROOT / "tsconfig.json"
# Each owns its own tsconfig or package, so the root one must not cover them.
FRONTEND = REPO_ROOT / "src" / "dispatch" / "static" / "dispatch"
DOCS = REPO_ROOT / "docs"

# tsconfig.json is JSONC, which `json` rejects. String literals are matched
# first so a `//` inside one survives.
_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _load_tsconfig() -> dict:
    text = _COMMENT.sub(lambda m: m[0] if m[0].startswith('"') else "", TSCONFIG.read_text())
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - only a malformed tsconfig
        raise AssertionError(f"{TSCONFIG.name} is not parseable JSONC: {exc}") from exc


def _covered() -> set[pathlib.Path]:
    """The files the root tsconfig's `include` patterns resolve to."""
    return {
        path
        for pattern in _load_tsconfig()["include"]
        for path in REPO_ROOT.glob(pattern)
        if path.is_file()
    }


def _repo_typescript() -> set[pathlib.Path]:
    """Every TypeScript file this repository authors outside the frontend and docs packages."""
    # .tsx as well as .ts: the guard is deliberately wider than the `include`
    # it checks, so a file the patterns cannot match fails here rather than
    # going quietly unchecked.
    return {
        path
        for suffix in ("*.ts", "*.tsx")
        for path in REPO_ROOT.rglob(suffix)
        if "node_modules" not in path.parts
        and not path.is_relative_to(FRONTEND)
        and not path.is_relative_to(DOCS)
    }


def test_every_e2e_typescript_file_is_type_checked():
    """Given the e2e tree, when a TypeScript file is added to it, then the root tsconfig covers it."""
    uncovered = sorted(str(p.relative_to(REPO_ROOT)) for p in _repo_typescript() - _covered())
    assert not uncovered, (
        f"outside every tsconfig, so `npm run typecheck:e2e` never sees them: {uncovered}"
    )


def test_the_root_tsconfig_does_not_reach_into_the_frontend_package():
    """The frontend has its own tsconfig with its own `types`; widening past it would check
    those files a second time under the wrong compiler options."""
    trespassing = sorted(
        str(p.relative_to(REPO_ROOT)) for p in _covered() if p.is_relative_to(FRONTEND)
    )
    assert not trespassing


def test_the_e2e_typecheck_is_runnable():
    """A tsconfig nothing invokes is an editor-only guard. The script is what makes it a
    command a contributor and a reviewer can actually run."""
    scripts = json.loads((REPO_ROOT / "package.json").read_text())["scripts"]
    assert "tsc" in scripts.get("typecheck:e2e", "")
