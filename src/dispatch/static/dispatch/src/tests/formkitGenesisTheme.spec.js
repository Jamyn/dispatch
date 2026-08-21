import { createRequire } from "module"
import fs from "fs"
import { mount } from "@vue/test-utils"
import { beforeAll, describe, expect, test, vi } from "vitest"
import { plugin, defaultConfig, FormKit } from "@formkit/vue"
import { defineComponent } from "vue"

// main.js styles every FormKit input with `@formkit/themes/genesis`, but the
// markup those styles target is emitted by `@formkit/vue`, which pins its own
// copy of @formkit/themes. The two are separate package.json entries on
// different majors, so nothing but this test notices if a bump to either side
// leaves the stylesheet aiming at classes FormKit no longer renders -- forms
// would just come out unstyled, which no other check looks at.
//
// This is a forward-looking guard, not evidence for any particular bump: every
// assertion below passes on 1.6.9 and 2.1.2 alike.

// Read through the package's exports map, so this resolves the same file
// main.js imports rather than a path that happens to exist today.
const genesisCss = fs.readFileSync(
  createRequire(import.meta.url).resolve("@formkit/themes/genesis"),
  "utf8",
)

// Genesis writes attribute values unquoted; a build that started quoting them
// would be a reformat, not a contract change, so normalize before matching.
const normalizedCss = genesisCss.replace(/(\[[\w-]+=)(["'])(.*?)\2/g, "$1$3")

// A hook is "styled" if the stylesheet names it as a whole token. Genesis wraps
// most compounds in :is(), so matching the text is what maps a hook to a rule
// without reimplementing a CSS parser -- but it has to stop at a token
// boundary, or `.formkit-message` would be satisfied by the unrelated
// `.formkit-messages` rule and assert nothing.
const styles = (hook) =>
  new RegExp(`${hook.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w-])`).test(normalizedCss)

// The input types Dispatch can actually render. Every `$formkit` value in the
// frontend is a string literal in forms/store.js and plugin/store.js -- server
// data supplies labels, options and defaults, never the type -- so this list is
// the whole reachable set. `taglist` and `dropdown` are also literals there but
// are FormKit Pro inputs, and Pro registration is commented out in main.js.
const Harness = defineComponent({
  components: { FormKit },
  template: `
    <FormKit type="form" submit-label="Save">
      <FormKit type="text" name="title" label="Title" help="Some help text" validation="required" />
      <FormKit type="text" name="frozen" label="Frozen" disabled="true" />
      <FormKit type="date" name="when" label="When" />
      <FormKit type="checkbox" name="agree" label="Agree" />
      <FormKit type="select" name="pick" label="Pick" placeholder="Choose" :options="['x', 'y']" />
    </FormKit>`,
})

let wrapper

beforeAll(async () => {
  wrapper = mount(Harness, { global: { plugins: [[plugin, defaultConfig]] } })
  // Empty out the required field so the invalid/message markup renders too.
  const title = wrapper.find('input[name="title"]')
  await title.setValue("x")
  await title.setValue("")
  await title.trigger("blur")
  // FormKit debounces validation rather than resolving it on the next tick, so
  // poll for the message instead of sleeping a fixed interval.
  await vi.waitUntil(() => wrapper.find(".formkit-message").exists(), { timeout: 5000 })
})

describe("the genesis stylesheet targets the markup FormKit renders", () => {
  test.each([
    ["formkit-outer", "per-input wrapper"],
    ["formkit-wrapper", "label/input grouping"],
    ["formkit-label", "labels"],
    ["formkit-inner", "input frame"],
    ["formkit-input", "the control itself"],
    ["formkit-help", "help text"],
    ["formkit-messages", "message list"],
    ["formkit-message", "validation messages"],
    ["formkit-actions", "submit row"],
  ])("%s (%s) is both rendered and styled", (cls) => {
    expect(wrapper.find(`.${cls}`).exists()).toBe(true)
    expect(styles(`.${cls}`)).toBe(true)
  })

  test.each([["checkbox"], ["select"], ["submit"]])(
    "the %s input carries the data-type genesis styles it by",
    (type) => {
      expect(wrapper.find(`[data-type="${type}"]`).exists()).toBe(true)
      expect(styles(`[data-type=${type}]`)).toBe(true)
    },
  )

  test("a disabled input carries the data-disabled genesis dims it by", () => {
    expect(wrapper.find("[data-disabled]").exists()).toBe(true)
    expect(styles("[data-disabled]")).toBe(true)
  })

  // Genesis has no [data-type=text] or [data-type=date] rule: text-like
  // controls are styled through .formkit-input, asserted above, and
  // [data-family=text] carries nothing but the two ::selection rules. Pinning
  // the family attribute is what keeps those two reachable.
  test.each([["text"], ["date"]])("the %s input is in the text family", (type) => {
    expect(wrapper.find(`[data-type="${type}"]`).attributes("data-family")).toBe("text")
  })

  test("the ::selection rules keyed off the text family still exist", () => {
    expect(styles("[data-family=text]")).toBe(true)
    expect(normalizedCss).toContain("[data-family=text] .formkit-input::selection")
  })

  // Error state is two separate contracts, and pairing them would assert
  // nothing: FormKit marks the failing input's *outer* with data-invalid, while
  // genesis's only [data-invalid] rule suppresses the focus ring on a
  // checkbox/radio inner. Dispatch's visible error styling is .formkit-message.
  test("FormKit marks a failed input's outer invalid", () => {
    const outer = wrapper.find(".formkit-outer[data-invalid]")
    expect(outer.exists()).toBe(true)
    expect(outer.find(".formkit-message").exists()).toBe(true)
  })

  test("genesis still scopes its [data-invalid] rule to checkbox and radio", () => {
    expect(styles("[data-invalid]")).toBe(true)
    // Bounded at the comma so it cannot drift into the sibling [data-errors] selector.
    expect(normalizedCss).toMatch(/\[data-invalid\][^{,\n]*data-type=(checkbox|radio)/)
  })

  // index.scss overrides --fk-color-input and --fk-color-help per Vuetify
  // theme. Those overrides only reach anything because genesis reads the two
  // variables back out on the same elements.
  test("the CSS variables index.scss overrides are still consumed", () => {
    expect(genesisCss).toContain("var(--fk-color-input)")
    expect(genesisCss).toContain("var(--fk-color-help)")
  })
})
