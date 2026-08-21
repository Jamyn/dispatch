// Pins the timezone conversions PreciseDateTimePicker performs, across the
// date-fns-tz 1.x -> 3.x rename (zonedTimeToUtc -> fromZonedTime,
// utcToZonedTime -> toZonedTime). Every expectation below was verified against
// both versions and is identical on each, so a change here is a real
// behavioural regression rather than a library difference.
process.env.TZ = "UTC"

import { mount } from "@vue/test-utils"
import { expect, test, describe, beforeAll } from "vitest"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import PreciseDateTimePicker from "@/components/PreciseDateTimePicker.vue"

const vuetify = createVuetify({ components, directives })

beforeAll(() => {
  // Two of the paths below render through the system zone, so a spec that
  // silently ran under another one would assert the wrong thing.
  expect(new Date().getTimezoneOffset()).toBe(0)
})

function picker(modelValue, timezone) {
  return mount(PreciseDateTimePicker, {
    props: { modelValue, timezone },
    global: { plugins: [vuetify] },
  })
}

describe("local time in a zone -> UTC instant", () => {
  // The unix timestamp is an absolute instant. That makes every row below
  // host-zone independent except the ambiguous one, whose resolution does vary
  // with the host zone -- identically in 1.3.8 and 3.2.0 -- which is why the
  // suite pins the zone rather than relying on the values being intrinsic.
  test.each([
    ["positive offset", "Asia/Kolkata", "2024-01-15T10:30:00", 1705294800000],
    ["negative offset", "America/Los_Angeles", "2024-01-15T10:30:00", 1705343400000],
    ["UTC", "UTC", "2024-01-15T10:30:00", 1705314600000],
    // 00:00 in +05:30 belongs to the previous UTC day.
    ["midnight rolls the UTC date back", "Asia/Kolkata", "2024-01-15T00:00:00", 1705257000000],
    // 02:30 never happens on this date -- the clocks jump 02:00 -> 03:00.
    // Resolved with the post-transition offset (EDT, -04:00).
    ["DST gap", "America/New_York", "2024-03-10T02:30:00", 1710052200000],
    // 01:30 happens twice; under a UTC host the first (EDT, -04:00) is chosen.
    ["DST ambiguity", "America/New_York", "2024-11-03T01:30:00", 1730611800000],
  ])("%s", (_label, timezone, local, expected) => {
    expect(picker(local, timezone).vm.unixTimestamp).toBe(expected)
  })
})

describe("UTC instant -> local time in a zone", () => {
  // handlePaste turns a pasted unix timestamp back into the zone's wall time.
  test.each([
    ["positive offset", "Asia/Kolkata", "2024-01-15T16:00:00"],
    ["negative offset", "America/Los_Angeles", "2024-01-15T02:30:00"],
    ["UTC", "UTC", "2024-01-15T10:30:00"],
  ])("%s", (_label, timezone, expected) => {
    const wrapper = picker("2024-01-15T10:30:00", timezone)
    wrapper.vm.handlePaste({
      preventDefault() {},
      clipboardData: { getData: () => "1705314600" },
    })
    const { year, month, day, hour, minutes, seconds } = wrapper.vm
    expect(`${year}-${month}-${day}T${hour}:${minutes}:${seconds}`).toBe(expected)
  })
})

describe("okHandler", () => {
  test("a UTC picker emits its fields untouched", () => {
    const wrapper = picker("2024-01-15T10:30:00", "UTC")
    wrapper.vm.okHandler()
    // No conversion is applied for UTC, so no offset can be introduced.
    expect(wrapper.emitted()["update:modelValue"][0][0]).toBe("2024-01-15T10:30:00")
  })

  test("a zoned picker emits the corresponding UTC instant", () => {
    const wrapper = picker("2024-01-15T10:30:00", "Asia/Kolkata")
    wrapper.vm.okHandler()
    expect(wrapper.emitted()["update:modelValue"][0][0]).toBe("2024-01-15T05:00:00.000")
  })
})
