// npm records dev/devOptional on every lockfile entry, and `--omit=dev` trusts
// those flags rather than re-resolving. A targeted lockfile edit that does not
// recompute them -- Dependabot writes one for every group bump -- silently
// moves build tooling into the production tree. `npm ci` validates the manifest
// against the lockfile, not the flags, so no existing gate sees it.
//
// Kept out of src/ so lockfile-sync can run it with no node_modules installed:
// it parses JSON and nothing else. src/tests/lockfileDevFlags.spec.js imports
// the same functions, so the logic has one home and stays unit-tested.

import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))

export const FRONTEND_LOCKFILE = resolve(HERE, "..", "package-lock.json")

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

export const productionClosure = (packages) => {
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

export const readLockfile = (path) => JSON.parse(readFileSync(path, "utf8"))

export const auditLockfile = (lock) => {
  const packages = lock.packages
  const production = productionClosure(packages)
  const unflagged = Object.keys(packages).filter(
    (path) =>
      path !== "" && !production.has(path) && !packages[path].dev && !packages[path].devOptional,
  )
  const misflagged = [...production].filter((path) => packages[path].dev)
  return { packages, production, unflagged, misflagged }
}

const SAMPLE = 10

// Exit non-zero with the regeneration command rather than a bare diff: this
// runs in lockfile-sync, where the reader has no test output to interpret.
const main = (argv) => {
  const lockPath = argv[2] ?? FRONTEND_LOCKFILE
  const { unflagged, misflagged } = auditLockfile(readLockfile(lockPath))
  const dir = dirname(lockPath)

  const report = (paths, problem) => {
    if (!paths.length) return false
    console.error(`${lockPath}: ${paths.length} package(s) ${problem}.`)
    for (const path of paths.slice(0, SAMPLE)) console.error(`  ${path}`)
    if (paths.length > SAMPLE) console.error(`  ... and ${paths.length - SAMPLE} more`)
    return true
  }

  const bad =
    report(unflagged, "production cannot reach are missing their dev flag") ||
    report(misflagged, "production depends on are marked dev")

  if (bad) {
    console.error(`\nRegenerate the lockfile so npm recomputes the flags:`)
    console.error(`  (cd ${dir} && npm install --package-lock-only)`)
    return 1
  }

  console.log(`${lockPath}: dev flags consistent`)
  return 0
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(main(process.argv))
}
