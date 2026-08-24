import { mount, flushPromises } from "@vue/test-utils"
import { expect, test, describe, afterEach } from "vitest"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import { createStore } from "vuex"
import { getField, updateField } from "vuex-map-fields"

import IncidentEditEventDialog from "@/incident/EditEventDialog.vue"
import CaseEditEventDialog from "@/case/EditEventDialog.vue"

const vuetify = createVuetify({ components, directives })

const STARTED_AT = "2023-01-01T10:00:00.000Z"

function createMockStore(namespace) {
  return createStore({
    modules: {
      [namespace]: {
        namespaced: true,
        state: {
          dialogs: { showEditEventDialog: true },
          selected: {
            currentEvent: {
              started_at: STARTED_AT,
              description: "an existing event",
              uuid: "d3b07384-d9a0-4c9b-8f2a-000000000000",
            },
          },
        },
        getters: { getField },
        mutations: { updateField },
        actions: {
          closeEditEventDialog() {},
          storeNewEvent() {},
          updateExistingEvent() {},
        },
      },
    },
  })
}

let wrappers = []

afterEach(() => {
  wrappers.forEach((w) => w.unmount())
  wrappers = []
})

async function mountDialog(component, namespace) {
  const wrapper = mount(component, {
    global: {
      plugins: [vuetify, createMockStore(namespace)],
    },
    // VDialog teleports its content into an overlay container appended to
    // body; without a real document parent the dialog body never mounts.
    attachTo: document.body,
  })
  await flushPromises()
  wrappers.push(wrapper)
  return wrapper
}

// Regression test for #283/#281: the incident copy's handler assigned an
// undeclared `eventStart`, so the button silently did nothing.
describe.each([
  ["incident", IncidentEditEventDialog, "incident"],
  ["case", CaseEditEventDialog, "case_management"],
])("%s EditEventDialog Now button", (_label, component, namespace) => {
  test("writes the current time to the property the picker is bound to", async () => {
    const wrapper = await mountDialog(component, namespace)
    expect(wrapper.vm.local_started_at).toBe(STARTED_AT)

    const nowButton = wrapper
      .findAllComponents({ name: "VBtn" })
      .find((btn) => btn.text().trim() === "Now")
    expect(nowButton).toBeDefined()

    const before = Date.now()
    await nowButton.trigger("click")
    await flushPromises()

    expect(wrapper.vm.local_started_at).toBeInstanceOf(Date)
    expect(wrapper.vm.local_started_at.getTime()).toBeGreaterThanOrEqual(before)
    expect(wrapper.vm.local_started_at.getTime()).toBeLessThanOrEqual(Date.now())
  })

  test("propagates the new time to the bound picker and the save path", async () => {
    const wrapper = await mountDialog(component, namespace)

    await wrapper.vm.setTimeToNow()
    await flushPromises()

    // Assert against Date rather than local_started_at: both stay equal to the
    // untouched STARTED_AT string when the handler writes the wrong property.
    const picker = wrapper.findComponent({ name: "DateTimePickerMenu" })
    expect(picker.props("modelValue")).toBeInstanceOf(Date)
    expect(picker.props("modelValue")).toBe(wrapper.vm.local_started_at)

    wrapper.vm.updateEvent()
    expect(wrapper.vm.started_at).toBeInstanceOf(Date)
    expect(wrapper.vm.started_at).toBe(wrapper.vm.local_started_at)
  })
})
