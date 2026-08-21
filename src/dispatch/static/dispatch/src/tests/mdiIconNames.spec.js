import { expect, describe, it } from "vitest"
import { readdirSync, readFileSync } from "node:fs"
import { createRequire } from "node:module"
import { dirname, join, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"

import iconCatalog from "../assets/icons"

// An `mdi-*` name that no longer exists in @mdi/font is not a build or lint
// error -- it is a CSS class with no rule behind it, so the icon renders as a
// blank box and every other check stays green. Only diffing the names used
// against the names the shipped font defines catches it.

const require = createRequire(import.meta.url)
const TESTS = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(TESTS, "..")

const definedIcons = (() => {
  const css = readFileSync(require.resolve("@mdi/font/css/materialdesignicons.css"), "utf8")
  return new Set(Array.from(css.matchAll(/\.(mdi-[a-z0-9-]+)::before/g), (m) => m[1]))
})()

const usedIcons = (() => {
  const found = new Map()
  for (const entry of readdirSync(SRC, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.(vue|js|ts)$/.test(entry.name)) continue
    const path = join(entry.parentPath, entry.name)
    // The specs are not shipped UI, and this one names invalid icons on purpose.
    if (path.startsWith(TESTS + sep)) continue
    // Template literals (`mdi-${...}`) build names from data and cannot be
    // resolved statically; requiring a letter after the dash skips them.
    for (const [name] of readFileSync(path, "utf8").matchAll(/\bmdi-[a-z0-9]+(?:-[a-z0-9]+)*/g)) {
      if (!found.has(name)) found.set(name, path)
    }
  }
  return found
})()

describe("mdi icon names used in src/", () => {
  it("resolves the shipped font's icon classes", () => {
    // Without this the probe silently measures an empty set and passes.
    expect(definedIcons.size).toBeGreaterThan(1000)
    expect(definedIcons.has("mdi-account")).toBe(true)
    expect(definedIcons.has("mdi-alert-minus-outline")).toBe(true)
    // Both were used in src/ until #204; neither resolves in the shipped font.
    expect(definedIcons.has("mdi-person")).toBe(false)
    expect(definedIcons.has("mdi-alert-minute-outline")).toBe(false)
  })

  it("finds the literals to check", () => {
    expect(usedIcons.size).toBeGreaterThan(50)
  })

  it("every literal exists in the shipped font", () => {
    const missing = [...usedIcons]
      .filter(([name]) => !definedIcons.has(name))
      .map(([name, path]) => `${name} (${path})`)
    expect(missing).toEqual([])
  })
})

// The catalog stores bare names (`{ name: "abacus" }`) that IconPickerInput
// renders as `mdi-${name}`, so the literal scan above cannot see them. An
// unresolvable entry is offered to operators and persisted into tag_type.icon.
describe("the icon catalog behind IconPickerInput", () => {
  it("finds the names to check", () => {
    expect(iconCatalog.length).toBeGreaterThan(1000)
    expect(iconCatalog.every((icon) => typeof icon.name === "string")).toBe(true)
  })

  it("every name exists in the shipped font", () => {
    const missing = iconCatalog
      .map((icon) => icon.name)
      .filter((name) => !definedIcons.has(`mdi-${name}`))
    expect(missing).toEqual([])
  })
})
