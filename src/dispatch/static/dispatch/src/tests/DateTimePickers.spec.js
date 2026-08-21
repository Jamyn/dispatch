// Covers the date-fns-tz entry points the 1.x -> 3.x upgrade did NOT rename:
// `format` (with and without a timeZone option) and `formatInTimeZone`. Their
// names are unchanged, so nothing in the build or the type system would flag a
// behaviour change here -- only an assertion will. Every expectation was
// checked against both versions and matches on each.
process.env.TZ = "UTC"

import { mount } from "@vue/test-utils"
import { expect, test, describe, beforeAll } from "vitest"
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

describe("DateTimePicker renders the instant in the viewer's own zone", () => {
  // Its `format(..., { timeZone: "UTC" })` does NOT convert: date-fns-tz only
  // consults `timeZone` for the [xXOz] tokens, and this pattern has none, so
  // the option is inert and the value is rendered in the host zone. That is
  // pre-existing -- v1.3.8 behaves identically -- so it is pinned here as-is
  // rather than corrected under a dependency bump. The suite fixes the zone to
  // UTC above, which is the only reason these read as UTC.
  test.each([
    ["midday", "2024-01-15T10:30:00Z", "2024-01-15T10:30"],
    // Under a +05:30 viewer this same instant renders as 2024-01-16T05:00.
    ["late evening", "2024-01-15T23:30:00Z", "2024-01-15T23:30"],
  ])("%s", (_label, modelValue, expected) => {
    expect(mountWith(DateTimePicker, { modelValue }).vm.selectedDatetime).toBe(expected)
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
