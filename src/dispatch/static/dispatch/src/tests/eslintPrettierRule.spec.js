import { expect, describe, it } from "vitest"
import { ESLint } from "eslint"

// A prettier/prettier rule that stops reporting -- a plugin/prettier version
// mismatch, a .prettierrc that no longer resolves -- leaves `npm run lint`
// green while formatting drifts. These assert it is actually enforcing.
const lint = async (code, filePath) => {
  const [result] = await new ESLint().lintText(code, { filePath })
  return result.messages.filter((m) => m.ruleId === "prettier/prettier")
}

describe("prettier/prettier under the project eslint config", () => {
  it("reports a formatting violation in a .js file", async () => {
    const messages = await lint("const   x = {a:1}\n", "src/__prettier-probe.js")
    expect(messages).not.toHaveLength(0)
  })

  it("reports a formatting violation in a .vue block", async () => {
    const sfc = "<template>\n  <div>x</div>\n</template>\n\n<script>\nconst   y = 1\n</script>\n"
    const messages = await lint(sfc, "src/__PrettierProbe.vue")
    expect(messages).not.toHaveLength(0)
  })

  it("honors .prettierrc rather than prettier's defaults", async () => {
    // The repo sets semi: false, so a trailing semicolon is a violation and the
    // fix removes it. Under prettier's default (semi: true) this passes clean,
    // which is how a config-resolution regression would show up.
    const messages = await lint("const x = 1;\n", "src/__prettier-probe.js")
    expect(messages.map((m) => m.message)).toContain("Delete `;`")
  })
})
