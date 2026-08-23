import { expect, describe, it } from "vitest"
import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// npm records dev/devOptional on every lockfile entry, and `--omit=dev` trusts
// those flags rather than re-resolving. A targeted lockfile edit that does not
// recompute them -- Dependabot writes one for every group bump -- silently
// moves build tooling into the production tree. `npm ci` validates the manifest
// against the lockfile, not the flags, so no existing gate sees it.

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "../..")

const lock = JSON.parse(readFileSync(resolve(FRONTEND, "package-lock.json"), "utf8"))

// node_modules resolution: a request from `at` for `name` binds to the deepest
// node_modules on its ancestor chain that holds the name.
const resolveFrom = (packages, at, name) => {
  let scope = at
  for (;;) {
    const candidate = scope ? `${scope}/node_modules/${name}` : `node_modules/${name}`
    if (packages[candidate]) return candidate
    if (!scope) return null
    scope = scope.includes("/node_modules/")
      ? scope.slice(0, scope.lastIndexOf("/node_modules/"))
      : ""
  }
}

// Everything the production tree can reach. Required peers count, because npm
// installs them; peers marked optional in peerDependenciesMeta do not, and
// following those walks straight from vuetify into the whole build toolchain.
const edgesFrom = (entry) => {
  const optionalPeers = entry.peerDependenciesMeta ?? {}
  const requiredPeers = Object.keys(entry.peerDependencies ?? {}).filter(
    (name) => !optionalPeers[name]?.optional,
  )
  return [
    ...Object.keys(entry.dependencies ?? {}),
    ...Object.keys(entry.optionalDependencies ?? {}),
    ...requiredPeers,
  ]
}

const productionClosure = (packages) => {
  const root = packages[""]
  const seen = new Set()
  const queue = Object.keys({ ...root.dependencies, ...root.optionalDependencies })
    .map((name) => resolveFrom(packages, "", name))
    .filter(Boolean)

  while (queue.length) {
    const path = queue.pop()
    if (seen.has(path)) continue
    seen.add(path)
    const entry = packages[path]
    if (!entry) continue
    for (const name of edgesFrom(entry)) {
      const target = resolveFrom(packages, path, name)
      if (target && !seen.has(target)) queue.push(target)
    }
  }
  return seen
}

describe("the frontend lockfile's dev flags", () => {
  const packages = lock.packages
  const production = productionClosure(packages)

  const unflagged = Object.keys(packages).filter(
    (path) =>
      path !== "" && !production.has(path) && !packages[path].dev && !packages[path].devOptional,
  )

  it("marks every package that production cannot reach as dev", () => {
    // A failure here means `npm ci --omit=dev` would install these. Regenerate
    // with `npm install --package-lock-only` and commit the result.
    expect(unflagged).toEqual([])
  })

  it("does not mark anything production depends on as dev", () => {
    const misflagged = [...production].filter((path) => packages[path].dev)
    expect(misflagged).toEqual([])
  })

  // Guard the guard: the closure has to actually reach past the root, or both
  // assertions above pass by describing an empty production tree.
  it("resolves a production tree deep enough for the check to mean anything", () => {
    expect(production.size).toBeGreaterThan(100)
    expect(Object.keys(packages).length).toBeGreaterThan(production.size)
  })
})
