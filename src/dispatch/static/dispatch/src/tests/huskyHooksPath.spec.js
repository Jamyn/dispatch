import { expect, describe, it } from "vitest"
import { execFileSync } from "node:child_process"
import { accessSync, constants, mkdtempSync, readFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, isAbsolute, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// A core.hooksPath that is absolute, or that points at husky's generated
// .husky/_, breaks git worktrees: the generated directory is gitignored, so a
// fresh worktree has no hook there and git skips pre-commit with no warning.
// Nothing else in the suite notices -- a skipped hook fails no check.

const TESTS = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(TESTS, "../..")
const REPO_ROOT = resolve(FRONTEND, "../../../..")

const prepare = JSON.parse(readFileSync(resolve(FRONTEND, "package.json"), "utf8")).scripts.prepare

// The last assignment wins: husky sets the path first, prepare overrides it.
const configuredPath = (() => {
  const matches = Array.from(prepare.matchAll(/git config core\.hooksPath (\S+)/g))
  return matches.length ? matches[matches.length - 1][1] : null
})()

describe("the prepare script's core.hooksPath", () => {
  it("is set explicitly rather than left to husky", () => {
    expect(configuredPath).not.toBeNull()
  })

  it("is relative, so git resolves it per worktree", () => {
    expect(isAbsolute(configuredPath)).toBe(false)
  })

  it("points at the tracked .husky, not the gitignored .husky/_", () => {
    expect(resolve(REPO_ROOT, configuredPath)).toBe(resolve(FRONTEND, ".husky"))
  })

  it("resolves to a pre-commit hook git will execute", () => {
    const hook = resolve(REPO_ROOT, configuredPath, "pre-commit")
    expect(() => accessSync(hook, constants.X_OK)).not.toThrow()
  })

  // docker/Dockerfile runs `npm ci` without --ignore-scripts and .dockerignore
  // strips .git, so `git config` fails there. prepare must still exit 0 or the
  // image build breaks -- behind image-smoke, which is not a required check.
  it("still exits 0 where there is no git repository", () => {
    const dir = mkdtempSync(join(tmpdir(), "hookspath-"))
    const run = (cmd) => execFileSync("sh", ["-c", cmd], { cwd: dir, stdio: "ignore" })
    // Guard the guard: if the temp dir were inside a repo, git config would
    // succeed and this would assert nothing.
    expect(() => run("git rev-parse --git-dir")).toThrow()
    expect(() => run(prepare.slice(prepare.indexOf("git config")))).not.toThrow()
  })
})
