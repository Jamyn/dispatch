// The shortcut buttons write straight into signal_filter.expiration, a naive
// column the backend reads as UTC (signal/service.py compares it against
// datetime.now(timezone.utc), and the API echoes it back Z-suffixed whatever
// was sent). A host wall clock stored there expires late by the viewer's
// offset, so the emitted form is pinned here to a real UTC instant. Every case
// runs off UTC deliberately -- under a UTC host the old and new values differ
// only by the Z.
process.env.TZ = "Asia/Kolkata"

import { mount, flushPromises } from "@vue/test-utils"
import { expect, test, describe, beforeEach, afterEach } from "vitest"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import ExpirationInput from "@/signal/filter/ExpirationInput.vue"

const vuetify = createVuetify({ components, directives })

// The shortcut Dates are built in data(), so the host zone has to be set before
// the mount, not just before the click.
async function clickShortcut(index) {
  const wrapper = mount(ExpirationInput, {
    props: { modelValue: null },
    global: { plugins: [vuetify] },
    attachTo: document.body,
  })
  wrapper.vm.menu = true
  await flushPromises()
  const shortcut = wrapper.vm.expirationShortcuts[index]
  document.body.querySelectorAll(".v-list-item")[index].click()
  await flushPromises()
  const emitted = wrapper.emitted("update:modelValue")[0][0]
  wrapper.unmount()
  return { emitted, instant: shortcut.expiration, title: shortcut.title }
}

afterEach(() => {
  // Each mount teleports its menu into document.body; without this the next
  // test's querySelectorAll would index into the previous test's list.
  document.body.innerHTML = ""
})

describe("under a positive-offset host zone", () => {
  test("the host zone really is +05:30 here", () => {
    // Control: without this every assertion below could be running under UTC,
    // where the shift being guarded against is zero.
    expect(new Date().getTimezoneOffset()).toBe(-330)
  })

  test.each([
    ["5 min", 0],
    ["1 day", 6],
    ["60 days", 9],
  ])("%s emits its own instant", async (title, index) => {
    const emit = await clickShortcut(index)
    expect(emit.title).toBe(title)
    expect(emit.emitted).toBe(emit.instant.toISOString())
  })

  test("the emitted value carries a zone designator", async () => {
    // A zoneless string is stored verbatim into the naive column, so the
    // backend reads the host wall clock as though it were UTC.
    const { emitted } = await clickShortcut(0)
    expect(emitted).toMatch(/Z$/)
  })

  test("read as UTC, the emitted value is still the intended instant", async () => {
    const { emitted, instant } = await clickShortcut(0)
    // Appends the Z the backend effectively supplies when it reads the column.
    expect(Date.parse(emitted.replace(/Z?$/, "Z"))).toBe(instant.getTime())
  })
})

describe("under a negative-offset host zone", () => {
  beforeEach(() => {
    process.env.TZ = "America/Los_Angeles"
  })
  afterEach(() => {
    process.env.TZ = "Asia/Kolkata"
  })

  test("the host zone really is negative here", () => {
    expect(new Date().getTimezoneOffset()).toBeGreaterThan(0)
  })

  test("read as UTC, the emitted value is still the intended instant", async () => {
    const { emitted, instant } = await clickShortcut(0)
    expect(Date.parse(emitted.replace(/Z?$/, "Z"))).toBe(instant.getTime())
  })
})
