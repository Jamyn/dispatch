import { mount } from "@vue/test-utils"
import { createStore } from "vuex"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import { describe, expect, it, vi, afterEach, beforeEach } from "vitest"
import CaseStatusSelectGroup from "@/case/CaseStatusSelectGroup.vue"

const vuetify = createVuetify({
  components,
  directives,
})

global.ResizeObserver = require("resize-observer-polyfill")

describe("CaseStatusSelectGroup", () => {
  let actions
  let mockStore
  let wrapper

  beforeEach(() => {
    // Mock the store and actions
    actions = {
      addBeNotification: vi.fn(),
    }

    mockStore = createStore({
      modules: {
        notification_backend: {
          namespaced: true,
          actions,
        },
        case_management: {
          namespaced: true,
          state: {
            selected: {
              id: 1,
              status: "New",
            },
          },
        },
      },
    })

    // Mount the component
    wrapper = mount(CaseStatusSelectGroup, {
      props: {
        modelValue: {
          status: "New",
          created_at: "2022-01-01",
        },
      },
      global: {
        plugins: [mockStore, vuetify], // 👈
      },
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("mounts correctly", () => {
    expect(wrapper.exists()).toBe(true)
  })

  it("opens dialog on status click", async () => {
    // The case is in "New" (the first card); clicking a *different* status
    // opens the status-change dialog.
    await wrapper.findAll(".overlap-card")[1].trigger("click")
    expect(wrapper.vm.dialogVisible).toBe(true)
  })

  it("opens the already-selected dialog when clicking the active status", async () => {
    await wrapper.findAll(".overlap-card")[0].trigger("click")
    expect(wrapper.vm.dialogVisible).toBe(false)
    expect(wrapper.vm.alreadySelectedDialog).toBe(true)
  })
})
