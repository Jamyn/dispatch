import { expect, describe, it } from "vitest"
import { execFileSync } from "node:child_process"
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// core.hooksPath must resolve to the tracked .husky, never husky's generated
// .husky/_. That directory is gitignored, so a worktree -- which shares the
// main checkout's config -- finds no hook there and git skips pre-commit
// silently. Nothing else in the suite notices: a skipped hook fails no check.

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "../..")
const REPO_ROOT = resolve(FRONTEND, "../../../..")
const NESTED = "src/dispatch/static/dispatch"
const HOOKS_DIR = `${NESTED}/.husky`

const prepare = JSON.parse(readFileSync(resolve(FRONTEND, "package.json"), "utf8")).scripts.prepare

// Runs the real prepare script against a throwaway copy of the repo layout.
// husky is stubbed out: what is under test is the core.hooksPath handling
// around it, and the real binary would generate the .husky/_ we must not rely on.
const runPrepare = (env = {}) => {
  const root = mkdtempSync(join(tmpdir(), "hookspath-"))
  const bin = join(root, "bin")
  mkdirSync(bin)
  writeFileSync(join(bin, "husky"), "#!/usr/bin/env sh\nexit 0\n")
  chmodSync(join(bin, "husky"), 0o755)
  mkdirSync(join(root, HOOKS_DIR), { recursive: true })

  const run = (cmd, cwd) =>
    execFileSync("sh", ["-c", cmd], {
      cwd,
      encoding: "utf8",
      env: { ...process.env, HUSKY: "", ...env, PATH: `${bin}:${process.env.PATH}` },
    })

  return {
    root,
    run,
    prepare: () => run(prepare, join(root, NESTED)),
    hooksPath: () => run("git config core.hooksPath || true", root).trim(),
  }
}

describe("the frontend prepare script", () => {
  it("points core.hooksPath at the tracked .husky, relative so it resolves per worktree", () => {
    const t = runPrepare()
    t.run("git init -q .", t.root)
    expect(() => t.prepare()).not.toThrow()
    expect(t.hooksPath()).toBe(HOOKS_DIR)
  })

  it("leaves core.hooksPath alone when HUSKY=0", () => {
    const t = runPrepare({ HUSKY: "0" })
    t.run("git init -q .", t.root)
    expect(() => t.prepare()).not.toThrow()
    expect(t.hooksPath()).toBe("")
  })

  // The .git guard exists so the Docker build survives; it must not widen into
  // swallowing a real failure, which would leave hooks unset -- the very silent
  // misconfiguration this script exists to prevent. Root ignores file modes.
  it.skipIf(process.getuid?.() === 0)("surfaces a genuine git config failure", () => {
    const t = runPrepare()
    t.run("git init -q . && chmod 555 .git", t.root)
    try {
      expect(() => t.prepare()).toThrow()
    } finally {
      t.run("chmod 755 .git", t.root)
    }
  })

  // docker/Dockerfile runs `npm ci` without --ignore-scripts, and .dockerignore
  // strips .git, so prepare must still exit 0 with no repository present or the
  // image build breaks -- behind image-smoke, which is not a required check.
  it("still exits 0 where there is no git repository", () => {
    const t = runPrepare()
    // Guard the guard: inside a repo this would assert nothing.
    expect(() => t.run("git rev-parse --git-dir", t.root)).toThrow()
    expect(() => t.prepare()).not.toThrow()
  })
})

describe("the tracked pre-commit hook", () => {
  // git now executes this file directly instead of husky's _ wrapper. A hook
  // committed non-executable is skipped with a hint and exit 0, so the mode in
  // the index -- not the working tree -- is what protects a fresh checkout.
  it("is committed executable, or git would silently skip it", () => {
    const entry = execFileSync("git", ["ls-files", "-s", "--", `${HOOKS_DIR}/pre-commit`], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    })
    expect(entry.split(" ")[0]).toBe("100755")
  })

  it("starts with a shebang, since git no longer sources it through husky", () => {
    const hook = readFileSync(resolve(REPO_ROOT, HOOKS_DIR, "pre-commit"), "utf8")
    expect(hook.startsWith("#!")).toBe(true)
  })
})
