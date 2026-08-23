import { expect, describe, it } from "vitest"

// The check itself lives in scripts/check-lock-dev-flags.mjs so lockfile-sync
// can run it in seconds with no node_modules. This spec is what keeps that
// logic honest -- especially the last case, which is the reason the closure is
// computed rather than hardcoded.
import {
  FRONTEND_LOCKFILE,
  auditLockfile,
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
    const victim = unflaggableDevPackage(lock)
    delete lock.packages[victim].dev
    delete lock.packages[victim].devOptional

    expect(auditLockfile(lock).unflagged).toEqual([victim])
  })
})

// A dev package outside the production closure, chosen from the lockfile rather
// than named, so the test does not rot when that dependency is removed.
function unflaggableDevPackage(lock) {
  const { production } = auditLockfile(lock)
  const candidate = Object.keys(lock.packages).find(
    (path) => path !== "" && !production.has(path) && lock.packages[path].dev,
  )
  if (!candidate) throw new Error("no dev-only package found to mutate")
  return candidate
}
