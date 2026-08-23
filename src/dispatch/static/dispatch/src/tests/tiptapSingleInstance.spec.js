import { expect, describe, it } from "vitest"
import { readFileSync } from "node:fs"
import { execFileSync } from "node:child_process"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// tiptap's packages share module-level state: extensions built against one
// copy of @tiptap/core are not accepted by an Editor built against another,
// and two copies of @tiptap/pm means two ProseMirror schemas. Bumping only
// part of the tiptap set resolves cleanly and installs a nested second major
// alongside the first, so npm ci, lint, typecheck, vitest and the production
// build all pass while the editor is broken at runtime. Nothing else here
// looks at the shape of the installed tree, so this does.

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "../..")

const lock = JSON.parse(readFileSync(resolve(FRONTEND, "package-lock.json"), "utf8"))
const manifest = JSON.parse(readFileSync(resolve(FRONTEND, "package.json"), "utf8"))

const tiptapEntries = Object.entries(lock.packages).filter(([path]) =>
  path.includes("node_modules/@tiptap/"),
)

const nameOf = (path) => path.slice(path.lastIndexOf("node_modules/") + "node_modules/".length)

// Scoped subpath imports ("@tiptap/pm/state") resolve against the package, so
// keep the first two segments and drop the rest.
const packageOf = (specifier) => specifier.split("/").slice(0, 2).join("/")

// Anchored on the import specifier so that prose mentioning a tiptap package
// -- including the comments in this file -- is not mistaken for a dependency.
const imported = new Set(
  execFileSync(
    "grep",
    ["-rhoE", "--exclude-dir=tests", 'from "@tiptap/[^"]+"', resolve(FRONTEND, "src")],
    { encoding: "utf8" },
  )
    .split("\n")
    .filter(Boolean)
    .map((line) => packageOf(line.slice('from "'.length, -1))),
)

describe("the installed tiptap tree", () => {
  it("installs exactly one copy of every tiptap package", () => {
    const copies = new Map()
    for (const [path] of tiptapEntries) {
      const name = nameOf(path)
      copies.set(name, [...(copies.get(name) ?? []), path])
    }

    const duplicated = [...copies.entries()].filter(([, paths]) => paths.length > 1)
    expect(duplicated).toEqual([])
  })

  it("keeps every tiptap package on a single major version", () => {
    const majors = new Set(tiptapEntries.map(([, entry]) => entry.version.split(".")[0]))
    expect([...majors]).toHaveLength(1)
  })

  it("declares every tiptap package the source imports", () => {
    // A package that is only present transitively still imports fine until an
    // unrelated bump stops hoisting it, so every import must be backed by a
    // direct dependency.
    const declared = new Set(Object.keys(manifest.dependencies ?? {}))
    expect([...imported].filter((name) => !declared.has(name))).toEqual([])
  })

  // Guard the guard: if the filter stopped matching, every assertion above
  // would pass against an empty tree.
  it("actually found the tiptap packages it is checking", () => {
    expect(tiptapEntries.length).toBeGreaterThan(10)
  })
})
