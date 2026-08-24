import { mount } from "@vue/test-utils"
import { describe, expect, test } from "vitest"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import fs from "fs"
import path from "path"

const vuetify = createVuetify({ components, directives })

global.ResizeObserver = require("resize-observer-polyfill")

// Vuetify 3 handed VForm's default slot the raw refs, so `isValid.value` read
// the boolean. Vuetify 4 unwraps them, which turns `!isValid.value` into a
// permanent `true` -- every submit button in the app disabled, no error thrown.
const FormHarness = {
  template: `
    <v-form @submit.prevent v-slot="{ isValid }">
      <v-text-field v-model="name" :rules="[(v) => !!v || 'Required']" />
      <v-btn class="submit" :disabled="!isValid">Save</v-btn>
    </v-form>
  `,
  data: () => ({ name: "" }),
}

const mountHarness = () => mount(FormHarness, { global: { plugins: [vuetify] } })

describe("VForm default slot props", () => {
  test("isValid is the value, not a ref", async () => {
    const wrapper = mountHarness()
    await wrapper.vm.$nextTick()

    const slotValue = wrapper.findComponent({ name: "VForm" }).vm.isValid
    expect(slotValue === null || typeof slotValue === "boolean").toBe(true)
    expect(wrapper.find(".submit").attributes("disabled")).toBeDefined()
  })

  test("submit button enables once the form validates", async () => {
    const wrapper = mountHarness()
    await wrapper.findComponent({ name: "VForm" }).vm.validate()
    await wrapper.vm.$nextTick()
    expect(wrapper.find(".submit").attributes("disabled")).toBeDefined()

    await wrapper.setData({ name: "a name" })
    await wrapper.findComponent({ name: "VForm" }).vm.validate()
    await wrapper.vm.$nextTick()
    expect(wrapper.find(".submit").attributes("disabled")).toBeUndefined()
  })
})

describe("no template still reads VForm slot props as refs", () => {
  const SRC = path.resolve(__dirname, "..")

  const vueFiles = (dir) =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) return entry.name === "node_modules" ? [] : vueFiles(full)
      return entry.name.endsWith(".vue") ? [full] : []
    })

  test.each(["isValid", "errors", "isValidating", "isDisabled", "isReadonly"])(
    "%s is never dereferenced with .value",
    (prop) => {
      const offenders = vueFiles(SRC).filter((file) =>
        fs.readFileSync(file, "utf-8").includes(`${prop}.value`),
      )
      expect(offenders.map((f) => path.relative(SRC, f))).toEqual([])
    },
  )
})
