import { expect, describe, it } from "vitest"
import { ESLint } from "eslint"

// eslint.config.js composes four sources -- eslint:recommended, the vue and
// vuetify plugin configs, and the project's own rules block. A source that
// silently stops applying leaves `npm run lint` green, so each is probed by a
// violation only that source reports.
const lint = async (code, filePath) => {
  const [result] = await new ESLint().lintText(code, { filePath })
  return result.messages.map((m) => m.ruleId)
}

const sfc = (body) => `<template>\n${body}\n</template>\n`

describe("the flat config wires every rule source", () => {
  it("applies local rules from eslint-local-rules.js", async () => {
    const rules = await lint(sfc(`  <v-btn icon>x</v-btn>`), "src/__FlatProbe.vue")
    expect(rules).toContain("local-rules/icon-button-variant")
  })

  it("applies eslint-plugin-vue", async () => {
    const rules = await lint(sfc(`  <div v-for="i in 3">{{ i }}</div>`), "src/__FlatProbe.vue")
    expect(rules).toContain("vue/require-v-for-key")
  })

  it("applies eslint-plugin-vuetify", async () => {
    const rules = await lint(sfc(`  <v-alert dismissible>a</v-alert>`), "src/__FlatProbe.vue")
    expect(rules).toContain("vuetify/no-deprecated-props")
  })

  it("applies eslint:recommended with browser and node globals", async () => {
    const rules = await lint(
      "window.alert(String(process.env.NODE_ENV))\nundefinedGlobalThing()\n",
      "src/__flat-probe.js",
    )
    expect(rules).toEqual(["no-undef"])
  })

  it("applies the project rules block over the plugin defaults", async () => {
    // eslint-plugin-vue enables vue/attribute-hyphenation; only the project's
    // own rules block, which has to come last, turns it back off.
    const rules = await lint(sfc(`  <v-btn myProp="x">a</v-btn>`), "src/__FlatProbe.vue")
    expect(rules).not.toContain("vue/attribute-hyphenation")
  })
})
