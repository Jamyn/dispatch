import { expect, describe, it } from "vitest"
import { ESLint } from "eslint"
import { readdirSync, readFileSync } from "node:fs"
import { basename, dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// The .ts files went unlinted two ways at once. Only one half fails loudly:
// drop the *.ts config object and every .ts file reports a parse error, but
// drop the *.ts `files` pattern and `eslint src` just silently enumerates
// fewer files and exits 0.

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "../..")
const scripts = JSON.parse(readFileSync(resolve(FRONTEND, "package.json"), "utf8")).scripts

const lint = async (code) => {
  const [result] = await new ESLint().lintText(code, { filePath: "src/__ts-probe.ts" })
  return result.messages
}

describe("the eslint config on TypeScript files", () => {
  it("parses type syntax instead of falling through to espree", async () => {
    // Nothing at all: not a parse error, and not the "file ignored" warning
    // ESLint emits when no config object's `files` matches a .ts path.
    expect(await lint("export const f = (x: string): string => x\n")).toEqual([])
  })

  it("still runs rules there", async () => {
    const messages = await lint("const unused: number = 1\n")
    expect(messages.map((m) => m.ruleId)).toContain("@typescript-eslint/no-unused-vars")
  })

  it("does not read a function-type parameter name as an unused variable", async () => {
    // Base no-unused-vars sees `e` as a variable; it is type syntax. Left on
    // beside the type-aware rule it reports every callback signature there is.
    expect(await lint("export type Cb = (e: Event) => void\n")).toHaveLength(0)
  })
})

describe("the lint scripts", () => {
  it.each(["lint", "lint:fix"])("%s lints the src directory, not a pattern", (name) => {
    // A directory argument is enumerated by the config's `files` patterns; a
    // glob argument would bypass them and pick its own extensions.
    expect(scripts[name]).toMatch(/^eslint src( |$)/)
  })

  it("enumerate .ts files under a directory argument", async () => {
    const dir = resolve(FRONTEND, "src/composables")
    const tsFiles = readdirSync(dir).filter((f) => f.endsWith(".ts"))
    expect(tsFiles).not.toHaveLength(0)
    const results = await new ESLint().lintFiles([dir])
    expect(results.map((r) => basename(r.filePath))).toEqual(expect.arrayContaining(tsFiles))
  })
})
