/**
 * A snooze expiration must read back the way it was picked (#287).
 *
 * `ExpirationInput`'s readonly field used to bind the model value verbatim
 * (`2026-08-22T08:55:00.000Z`) while `TableInstanceSnoozes` rendered the same
 * instant through `formatDate` -- `formatISO(parseISO(v))`, which is host-local
 * with an offset. Both sides now render UTC with the zone named, matching the
 * picker inside the same menu and the zone the backend compares the column in.
 *
 * The host zone is deliberately +05:30: under UTC a local rendering and a UTC
 * one are the same string, so every assertion below would pass on the bug.
 */
process.env.TZ = "Asia/Kolkata"

import { mount, flushPromises } from "@vue/test-utils"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createStore } from "vuex"
import { createRouter, createMemoryHistory } from "vue-router"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"

vi.mock("@/signal/api", () => ({ default: { getAllInstances: vi.fn() } }))
vi.mock("@/signal/filter/api", () => ({ default: { getAll: vi.fn() } }))
vi.mock("@/entity/api", () => ({ default: { getAll: vi.fn() } }))

import signalInstanceStore from "@/signal/instance/store"
import ExpirationInput from "@/signal/filter/ExpirationInput.vue"
import TableInstanceSnoozes from "@/signal/instance/TableInstanceSnoozes.vue"

const vuetify = createVuetify({ components, directives })

const INSTANT = "2026-08-22T08:55:00.000Z"
const UTC_RENDERING = "2026-08-22 08:55:00 UTC"
// The same instant as a +05:30 wall clock. Nothing may render this.
const HOST_LOCAL_TIME = "14:25"

/** The readonly field's rendered value, which is what the user reads. */
async function inputRendering(modelValue) {
  const wrapper = mount(ExpirationInput, {
    props: { modelValue },
    global: { plugins: [vuetify] },
  })
  await flushPromises()
  const field = wrapper.find("input").element.value
  wrapper.unmount()
  return field
}

/** The expiration cell's tooltip text, which is the table's precise rendering. */
async function tableRendering(expiration) {
  const store = createStore({
    modules: {
      signalInstance: {
        namespaced: true,
        state: {
          ...signalInstanceStore.state,
          snoozeTable: {
            ...signalInstanceStore.state.snoozeTable,
            rows: { items: [{ id: 1, name: "snooze", description: "", expiration }], total: 1 },
            loading: false,
          },
        },
        getters: signalInstanceStore.getters,
        mutations: signalInstanceStore.mutations,
        actions: { ...signalInstanceStore.actions, getAllSnoozes: vi.fn() },
      },
      auth: {
        namespaced: true,
        state: { currentUser: { projects: [] } },
        getters: signalInstanceStore.getters,
        mutations: signalInstanceStore.mutations,
      },
    },
  })

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/", name: "snoozes", component: { template: "<div />" } }],
  })
  await router.push({ name: "snoozes" })
  await router.isReady()

  const wrapper = mount(TableInstanceSnoozes, {
    global: { plugins: [store, router, vuetify] },
    attachTo: document.body,
  })
  await flushPromises()
  // VTooltip is eager, so its content is in the DOM without hovering; the
  // activator holds the relative date and the overlay holds the precise value.
  const overlay = document.body.querySelector(".v-tooltip .v-overlay__content")
  const text = overlay ? overlay.textContent.trim() : null
  wrapper.unmount()
  return text
}

afterEach(() => {
  document.body.innerHTML = ""
})

describe("snooze expiration presentation", () => {
  it("really is running under a +05:30 host zone", () => {
    // Control: without this the UTC and local renderings coincide and none of
    // the assertions below can distinguish the fix from the bug.
    expect(new Date().getTimezoneOffset()).toBe(-330)
  })

  it("renders the picked value as a labelled UTC instant", async () => {
    expect(await inputRendering(INSTANT)).toBe(UTC_RENDERING)
  })

  it("renders the stored value in the table the same way", async () => {
    expect(await tableRendering(INSTANT)).toBe(UTC_RENDERING)
  })

  it("shows the user the same string on both sides", async () => {
    const [input, table] = [await inputRendering(INSTANT), await tableRendering(INSTANT)]
    expect(input).toBe(table)
    expect(input).not.toContain(HOST_LOCAL_TIME)
    expect(table).not.toContain(HOST_LOCAL_TIME)
  })

  it("renders an empty field rather than throwing when there is no expiration", async () => {
    // The field is cleared far more often than it is set, and `parseISO` throws
    // on null under date-fns 4 (#256) rather than returning an invalid date.
    expect(await inputRendering(null)).toBe("")
  })
})
