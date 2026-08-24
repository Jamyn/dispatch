import { expect, describe, it } from "vitest"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

// apexcharts relicensed away from MIT partway through its 5.x line. 4.7.0 is
// the last release whose LICENSE is the MIT text; 5.0.0 ships the revenue-
// gated ApexCharts License while still declaring "MIT" in package.json, so the
// registry metadata alone will not catch a bad bump. Dispatch is Apache-2.0
// and redistributes the built frontend, so this pin is a licensing boundary.
const LAST_MIT_MAJOR = 4

const read = (rel) => JSON.parse(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8"))

const pkg = read("../../package.json")
const lock = read("../../package-lock.json")

describe("the apexcharts pin", () => {
  const range = pkg.dependencies.apexcharts

  it("is an exact version, not a range", () => {
    // `^4.7.0` would be satisfied by nothing today, but `^5` / `~5.0` would
    // silently drift onto the relicensed releases.
    expect(range).toMatch(/^\d+\.\d+\.\d+$/)
  })

  it("stays on the last MIT-licensed major", () => {
    expect(Number(range.split(".")[0])).toBeLessThanOrEqual(LAST_MIT_MAJOR)
  })

  it("resolves in the lockfile to the pinned version", () => {
    expect(lock.packages["node_modules/apexcharts"].version).toBe(range)
  })
})
