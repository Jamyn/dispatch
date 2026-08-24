import { mount } from "@vue/test-utils"
import { describe, expect, test, vi } from "vitest"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import fs from "fs"
import path from "path"

const vuetify = createVuetify({ components, directives })

global.ResizeObserver = require("resize-observer-polyfill")

// Vuetify 4 renamed the select family's `item` slot prop to `internalItem` and
// made `item` its `.raw`. `item.raw.name` therefore reads undefined and renders
// an empty row -- no error, no failing type check.
vi.mock("@/case/priority/api", () => ({
  default: {
    getAll: vi.fn(() =>
      Promise.resolve({
        data: {
          items: [{ id: 1, name: "High", description: "Wake someone up" }],
          total: 1,
        },
      }),
    ),
  },
}))

import CasePrioritySelect from "@/case/priority/CasePrioritySelect.vue"

const flush = async (wrapper) => {
  for (let i = 0; i < 5; i++) await wrapper.vm.$nextTick()
}

describe("select item slot", () => {
  test("renders the raw item's fields in the menu", async () => {
    const wrapper = mount(CasePrioritySelect, {
      props: { modelValue: {}, project: { id: 1, name: "default" } },
      global: { plugins: [vuetify] },
      attachTo: document.body,
    })
    await flush(wrapper)

    wrapper.findComponent({ name: "VSelect" }).vm.menu = true
    await flush(wrapper)

    const menu = document.body.textContent
    expect(menu).toContain("High")
    expect(menu).toContain("Wake someone up")

    wrapper.unmount()
  })
})

describe("no select template still reaches through item.raw", () => {
  const SRC = path.resolve(__dirname, "..")

  const vueFiles = (dir) =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) return entry.name === "node_modules" ? [] : vueFiles(full)
      return entry.name.endsWith(".vue") ? [full] : []
    })

  test("item.raw is absent from every file using VSelect/VCombobox/VAutocomplete", () => {
    const offenders = vueFiles(SRC).filter((file) => {
      const source = fs.readFileSync(file, "utf-8")
      return /<v-(select|combobox|autocomplete)\b/.test(source) && /\bitem\.raw\b/.test(source)
    })
    expect(offenders.map((f) => path.relative(SRC, f))).toEqual([])
  })
})
