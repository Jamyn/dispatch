// Covers the date-fns-tz entry points the 1.x -> 3.x upgrade did NOT rename:
// `format` (with and without a timeZone option) and `formatInTimeZone`. Their
// names are unchanged, so nothing in the build or the type system would flag a
// behaviour change here -- only an assertion will. Every expectation was
// checked against both versions and matches on each.
process.env.TZ = "UTC"

import { mount } from "@vue/test-utils"
import { expect, test, describe, beforeAll, beforeEach, afterEach } from "vitest"
import { nextTick } from "vue"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import DateTimePicker from "@/components/DateTimePicker.vue"
import DateTimePickerMenu from "@/components/DateTimePickerMenu.vue"

const vuetify = createVuetify({ components, directives })

beforeAll(() => {
  expect(new Date().getTimezoneOffset()).toBe(0)
})

const mountWith = (component, props) => mount(component, { props, global: { plugins: [vuetify] } })

describe("DateTimePicker renders the instant in UTC", () => {
  test.each([
    ["midday", "2024-01-15T10:30:00Z", "2024-01-15T10:30"],
    ["late evening", "2024-01-15T23:30:00Z", "2024-01-15T23:30"],
  ])("%s", (_label, modelValue, expected) => {
    expect(mountWith(DateTimePicker, { modelValue }).vm.selectedDatetime).toBe(expected)
  })
})

describe("DateTimePicker renders UTC under a positive-offset host zone", () => {
  // These are the cases a UTC-only suite cannot see: date-fns-tz's `format`
  // ignores a `timeZone` option unless the pattern carries an [xXOz] token, so
  // a pattern without one renders in the host zone whatever the option says.
  beforeEach(() => {
    process.env.TZ = "Asia/Kolkata"
  })
  afterEach(() => {
    process.env.TZ = "UTC"
  })

  test("the host zone really is +05:30 here", () => {
    // Control: without this, every assertion below would still be running
    // under UTC and would pass against the unconverted rendering.
    expect(new Date().getTimezoneOffset()).toBe(-330)
  })

  test("a late-evening instant keeps its UTC date", () => {
    expect(
      mountWith(DateTimePicker, { modelValue: "2024-01-15T23:30:00Z" }).vm.selectedDatetime,
    ).toBe("2024-01-15T23:30")
  })

  test("okHandler reads the field back as UTC", () => {
    // This one also passes before the fix -- the old render and the old
    // parse-back cancelled out. It guards the second half of the pair.
    const wrapper = mountWith(DateTimePicker, { modelValue: "2024-01-15T23:30:00Z" })
    wrapper.vm.okHandler()
    expect(wrapper.emitted("update:modelValue")[0]).toEqual(["2024-01-15T23:30:00.000Z"])
  })

  test("a Date prop renders in UTC too", () => {
    const modelValue = new Date("2024-01-15T23:30:00Z")
    expect(mountWith(DateTimePicker, { modelValue }).vm.selectedDatetime).toBe("2024-01-15T23:30")
  })

  test("a string with no zone designator is read as host-local first", () => {
    // parseISO resolves a zoneless string against the host zone, so
    // 05:00+05:30 renders as 23:30 UTC. No in-tree caller emits this shape
    // any more; this guards the parse path in case one reappears.
    expect(
      mountWith(DateTimePicker, { modelValue: "2024-01-16T05:00:00.000" }).vm.selectedDatetime,
    ).toBe("2024-01-15T23:30")
  })
})

describe("DateTimePicker emits what the user types", () => {
  // The field is the component's only emit path: nothing else invokes okHandler
  // or clearHandler, and its sole consumer binds v-model rather than the slot.
  const field = (wrapper) => wrapper.find('input[type="datetime-local"]')

  test("a typed datetime reaches the parent as a UTC instant", async () => {
    const wrapper = mountWith(DateTimePicker, { modelValue: null })
    await field(wrapper).setValue("2024-02-01T09:15")
    expect(wrapper.emitted("update:modelValue")).toEqual([["2024-02-01T09:15:00.000Z"]])
  })

  test("editing an existing value replaces it", async () => {
    const wrapper = mountWith(DateTimePicker, { modelValue: "2024-01-15T10:30:00Z" })
    await field(wrapper).setValue("2024-01-15T18:45")
    expect(wrapper.emitted("update:modelValue")).toEqual([["2024-01-15T18:45:00.000Z"]])
  })

  test("emptying the field clears the parent", async () => {
    const wrapper = mountWith(DateTimePicker, { modelValue: "2024-01-15T10:30:00Z" })
    await field(wrapper).setValue("")
    expect(wrapper.emitted("update:modelValue")).toEqual([[null]])
  })

  test("mounting with a value emits nothing", async () => {
    // init() writes selectedDatetime itself. Emitting from a watcher on it would
    // rewrite the parent's value on mount -- ExpirationInput's shortcuts store a
    // zoneless string, which would silently normalise to a Z-suffixed one.
    const wrapper = mountWith(DateTimePicker, { modelValue: "2024-01-16T05:00:00.000" })
    // A watcher on selectedDatetime flushes on nextTick, so a synchronous
    // assertion here would pass against the very design this rules out.
    await nextTick()
    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
  })

  test("a year the converter cannot parse is dropped, not thrown", () => {
    // Chrome's year segment reaches 275760; fromZonedTime returns Invalid Date
    // past four digits. happy-dom's input sanitiser rejects those values before
    // the DOM does, so the handler has to be driven directly here.
    const wrapper = mountWith(DateTimePicker, { modelValue: null })
    wrapper.vm.selectedDatetime = "275760-09-13T00:00"
    expect(() => wrapper.vm.okHandler()).not.toThrow()
    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
  })

  test("a parent-driven change emits nothing", async () => {
    const wrapper = mountWith(DateTimePicker, { modelValue: "2024-01-15T10:30:00Z" })
    await wrapper.setProps({ modelValue: "2024-01-15T18:45:00Z" })
    expect(wrapper.vm.selectedDatetime).toBe("2024-01-15T18:45")
    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
  })
})

describe("DateTimePickerMenu renders the instant in the selected zone", () => {
  test.each([
    ["positive offset", "Asia/Kolkata", "2024-01-15T10:30:00Z", "2024-01-15T16:00:00.000"],
    ["negative offset", "America/Los_Angeles", "2024-01-15T10:30:00Z", "2024-01-15T02:30:00.000"],
    ["UTC", "UTC", "2024-01-15T10:30:00Z", "2024-01-15T10:30:00.000"],
    // 07:00Z is the instant the US eastern clocks jump 02:00 -> 03:00.
    ["DST spring forward", "America/New_York", "2024-03-10T07:00:00Z", "2024-03-10T03:00:00.000"],
    // 05:30Z is inside the repeated eastern hour; the first pass (EDT) shows.
    ["DST fall back", "America/New_York", "2024-11-03T05:30:00Z", "2024-11-03T01:30:00.000"],
  ])("%s", (_label, timezone, modelValue, expected) => {
    expect(mountWith(DateTimePickerMenu, { modelValue, timezone }).vm.selectedDatetime).toBe(
      expected,
    )
  })
})

describe("DateTimePickerMenu survives a timezone change", () => {
  // The timezone watcher re-reads modelValue, whose type is [Date, String] with
  // a null default. date-fns 4 throws a TypeError on null and on a Date, so
  // both shapes have to reach the watcher, not just the string that works.
  test.each([
    ["a null modelValue", null, null],
    ["a Date modelValue", new Date("2024-01-15T10:30:00Z"), "2024-01-15T16:00:00.000"],
  ])("%s", async (_label, modelValue, expected) => {
    const wrapper = mountWith(DateTimePickerMenu, { modelValue, timezone: "UTC" })
    await wrapper.setProps({ timezone: "Asia/Kolkata" })
    expect(wrapper.vm.selectedDatetime).toBe(expected)
  })

  test("a string modelValue still re-renders in the new zone", async () => {
    // Passes before the fix too. It guards the behaviour the watcher exists
    // for, so a guard that simply stopped re-rendering would not pass silently.
    const wrapper = mountWith(DateTimePickerMenu, {
      modelValue: "2024-01-15T10:30:00Z",
      timezone: "UTC",
    })
    await wrapper.setProps({ timezone: "Asia/Kolkata" })
    expect(wrapper.vm.selectedDatetime).toBe("2024-01-15T16:00:00.000")
  })
})
