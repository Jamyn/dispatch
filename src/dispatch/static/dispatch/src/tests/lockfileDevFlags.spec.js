import { expect, describe, it, afterEach } from "vitest"
import { mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

// The check itself lives in scripts/check-lock-dev-flags.mjs so lockfile-sync
// can run it in seconds with no node_modules. This spec is what keeps that
// logic honest -- especially the cases that mutate a lockfile, which are the
// reason the closure is computed rather than hardcoded.
import {
  FRONTEND_LOCKFILE,
  auditLockfile,
  main,
  readLockfile,
} from "../../scripts/check-lock-dev-flags.mjs"

const { packages, production, unflagged, misflagged } = auditLockfile(
  readLockfile(FRONTEND_LOCKFILE),
)

describe("the frontend lockfile's dev flags", () => {
  it("marks every package that production cannot reach as dev", () => {
    // A failure here means `npm ci --omit=dev` would install these. Regenerate
    // with `npm install --package-lock-only` and commit the result.
    expect(unflagged).toEqual([])
  })

  it("does not mark anything production depends on as dev", () => {
    expect(misflagged).toEqual([])
  })

  // Guard the guard: the closure has to actually reach past the root, or both
  // assertions above pass by describing an empty production tree.
  it("resolves a production tree deep enough for the check to mean anything", () => {
    expect(production.size).toBeGreaterThan(100)
    expect(Object.keys(packages).length).toBeGreaterThan(production.size)
  })

  // The failure mode this whole file exists for: dropping `dev` from a package
  // production cannot reach has to be caught. Without this, a closure bug that
  // over-reports the production tree would silently disarm the check above.
  it("catches a dev flag dropped from an unreachable package", () => {
    const lock = readLockfile(FRONTEND_LOCKFILE)
    const victim = pick(lock, (path, entry, prod) => !prod.has(path) && entry.dev)
    delete lock.packages[victim].dev
    delete lock.packages[victim].devOptional

    expect(auditLockfile(lock).unflagged).toEqual([victim])
  })

  it("catches a production package wrongly marked dev", () => {
    const lock = readLockfile(FRONTEND_LOCKFILE)
    const victim = pick(lock, (path, entry, prod) => prod.has(path) && !entry.dev)
    lock.packages[victim].dev = true

    expect(auditLockfile(lock).misflagged).toEqual([victim])
  })
})

// What CI actually runs is the exit code, so it gets its own assertions rather
// than being inferred from the audit functions above.
describe("the lockfile dev-flag command", () => {
  const dirs = []
  afterEach(() => {
    while (dirs.length) rmSync(dirs.pop(), { recursive: true, force: true })
  })

  const runOn = (mutate) => {
    const lock = readLockfile(FRONTEND_LOCKFILE)
    if (mutate) mutate(lock)
    const dir = mkdtempSync(join(tmpdir(), "lockgate-"))
    dirs.push(dir)
    const path = join(dir, "package-lock.json")
    writeFileSync(path, JSON.stringify(lock))

    const errors = []
    const logs = []
    const code = main(["node", "check-lock-dev-flags.mjs", path], {
      error: (m) => errors.push(m),
      log: (m) => logs.push(m),
    })
    return { code, err: errors.join("\n"), log: logs.join("\n") }
  }

  it("exits 0 on a lockfile whose flags are consistent", () => {
    const { code, log } = runOn(null)
    expect(code).toBe(0)
    expect(log).toContain("dev flags consistent")
  })

  it("exits 1 and names the regeneration command when a dev flag is dropped", () => {
    const { code, err } = runOn((lock) => {
      const victim = pick(lock, (path, entry, prod) => !prod.has(path) && entry.dev)
      delete lock.packages[victim].dev
      delete lock.packages[victim].devOptional
    })
    expect(code).toBe(1)
    expect(err).toContain("missing their dev flag")
    expect(err).toContain("npm install --package-lock-only")
  })

  it("exits 1 when production depends on something marked dev", () => {
    const { code, err } = runOn((lock) => {
      const victim = pick(lock, (path, entry, prod) => prod.has(path) && !entry.dev)
      lock.packages[victim].dev = true
    })
    expect(code).toBe(1)
    expect(err).toContain("marked dev")
  })
})

// Victims are chosen from the lockfile rather than named, so these tests do not
// rot when the dependency they happened to pick is removed.
function pick(lock, predicate) {
  const { production } = auditLockfile(lock)
  const found = Object.keys(lock.packages).find(
    (path) => path !== "" && predicate(path, lock.packages[path], production),
  )
  if (!found) throw new Error("no lockfile entry matched the mutation predicate")
  return found
}
