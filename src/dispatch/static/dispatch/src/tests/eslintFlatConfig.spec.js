import { expect, describe, it } from "vitest"
import { ESLint } from "eslint"

// eslint.config.js composes several sources -- eslint:recommended, the vue and
// vuetify plugin configs, the local rules, and the project's own rules block.
// Under flat config a source that drops out (a plugin object not registered, a
// spread left off, an object in the wrong order) leaves `npm run lint` green,
// so each is probed by a finding only that source produces.
const lint = async (code, filePath) => {
  const [result] = await new ESLint().lintText(code, { filePath })
  return result.messages
}
const ruleIds = async (code, filePath) => (await lint(code, filePath)).map((m) => m.ruleId)

const sfc = (template, script = "") =>
  `<template>\n${template}\n</template>\n${script ? `\n${script}\n` : ""}`

describe("the flat config wires every rule source", () => {
  it("applies local rules from eslint-local-rules.js", async () => {
    const rules = await ruleIds(sfc(`  <v-btn icon>x</v-btn>`), "src/__FlatProbe.vue")
    expect(rules).toContain("local-rules/icon-button-variant")
  })

  it("applies local rules to a <script lang='ts'> SFC too", async () => {
    // The local rules only visit template nodes, and they reach them through
    // parserServices. Handing the script block to @typescript-eslint/parser
    // must not cost the template its services, or the rules go quiet on the
    // 24 SFCs that use lang="ts" while `npm run lint` stays green.
    const rules = await ruleIds(
      sfc(
        `  <v-btn icon>x</v-btn>`,
        `<script lang="ts">\nexport const f = (x: string): string => x\n</script>`,
      ),
      "src/__FlatProbe.vue",
    )
    expect(rules).toContain("local-rules/icon-button-variant")
  })

  it("enables only the one local rule the config names", async () => {
    // eslint-local-rules.js defines three rules; two are commented out in
    // eslint.config.js on purpose. eslint-plugin-local-rules 3 ships a
    // `configs.all` that turns on every rule it discovers, so adopting it
    // would switch those two back on without touching the rules block.
    const config = await new ESLint().calculateConfigForFile("src/App.vue")
    const enabled = Object.keys(config.rules).filter((id) => id.startsWith("local-rules/"))
    expect(enabled).toEqual(["local-rules/icon-button-variant"])
  })

  it("applies eslint-plugin-vue", async () => {
    const rules = await ruleIds(sfc(`  <div v-for="i in 3">{{ i }}</div>`), "src/__FlatProbe.vue")
    expect(rules).toContain("vue/require-v-for-key")
  })

  it("applies eslint-plugin-vuetify", async () => {
    const rules = await ruleIds(sfc(`  <v-alert dismissible>a</v-alert>`), "src/__FlatProbe.vue")
    expect(rules).toContain("vuetify/no-deprecated-props")
  })

  it("applies eslint:recommended with browser and node globals", async () => {
    const rules = await ruleIds(
      "window.alert(String(process.env.NODE_ENV))\nundefinedGlobalThing()\n",
      "src/__flat-probe.js",
    )
    expect(rules).toEqual(["no-undef"])
  })

  it("parses <script lang='ts'> in an SFC", async () => {
    // vue-eslint-parser only hands script blocks to @typescript-eslint/parser
    // if parserOptions.parser reaches the *.vue config; otherwise this is a
    // fatal parse error.
    const messages = await lint(
      sfc(`  <div />`, `<script lang="ts">\nexport const f = (x: string): string => x\n</script>`),
      "src/__FlatProbe.vue",
    )
    expect(messages.filter((m) => m.fatal)).toHaveLength(0)
  })

  it("keeps vue's formatting rules that prettier would switch off", async () => {
    // eslint-config-prettier turns vue/html-quotes off; it is on because the
    // vue config is applied after prettier, as under the old .eslintrc.
    const rules = await ruleIds(sfc(`  <div class='x'></div>`), "src/__FlatProbe.vue")
    expect(rules).toContain("vue/html-quotes")
  })

  it("applies the project rules block over the plugin defaults", async () => {
    // eslint-plugin-vue enables vue/attribute-hyphenation; only the project's
    // own rules block, which has to come last, turns it back off.
    const rules = await ruleIds(sfc(`  <v-btn myProp="x">a</v-btn>`), "src/__FlatProbe.vue")
    expect(rules).not.toContain("vue/attribute-hyphenation")
  })
})
