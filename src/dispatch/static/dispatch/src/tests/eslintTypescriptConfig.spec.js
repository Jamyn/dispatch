import { expect, describe, it } from "vitest"
import { ESLint } from "eslint"
import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// The .ts files went unlinted two ways at once. Only one half fails loudly:
// drop the *.ts config object and every .ts file reports a parse error, but
// drop .ts from --ext and the file set just silently shrinks and exits 0.

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "../..")
const scripts = JSON.parse(readFileSync(resolve(FRONTEND, "package.json"), "utf8")).scripts
const extensions = (script) => (script.match(/--ext\s+(\S+)/)?.[1] ?? "").split(",")

const lint = async (code) => {
  const [result] = await new ESLint().lintText(code, { filePath: "src/__ts-probe.ts" })
  return result.messages
}

describe("the eslint config on TypeScript files", () => {
  it("parses type syntax instead of falling through to espree", async () => {
    const messages = await lint("export const f = (x: string): string => x\n")
    expect(messages.filter((m) => m.fatal)).toHaveLength(0)
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
  it.each(["lint", "lint:fix"])("enumerate .ts in %s", (name) => {
    expect(extensions(scripts[name])).toContain(".ts")
  })
})
