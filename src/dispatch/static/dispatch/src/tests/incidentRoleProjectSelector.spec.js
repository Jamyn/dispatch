/**
 * The Incident Roles project selector must rescope the role policies (#289).
 *
 * `incident_role/Table.vue` assigned `breadCrumbProject` and `project` in
 * `created()` without declaring either in `data()`. Vue 3 puts such an
 * assignment on the instance's `ctx`, which renders but is not reactive, so the
 * breadcrumb watcher never fired and `:project` never reached the three
 * `PolicyRoleBuilder` children.
 *
 * `PolicyRoleBuilder` is mounted for real rather than stubbed: a reactive
 * parent property is not proof the child refetches -- the child's own
 * `watch(() => props.project)` is the behaviour under test, and it dereferences
 * `props.project.name` unguarded.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"
import { mount, flushPromises } from "@vue/test-utils"
import { createRouter, createMemoryHistory } from "vue-router"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"

vi.mock("@/incident_role/api", () => ({
  default: { getRolePolicies: vi.fn(), updateRole: vi.fn() },
}))

import IncidentRoleApi from "@/incident_role/api"
import IncidentRoleTable from "@/incident_role/Table.vue"

const vuetify = createVuetify({ components, directives })

/**
 * `flushPromises` alone leaves the router mid-navigation once the full Vuetify
 * tree is mounted, so a URL assertion would read the pre-push route. The router
 * needs a macrotask turn as well before it commits.
 */
async function settle() {
  await flushPromises()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await flushPromises()
}

async function mountTable(query = {}) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: "/settings/incident-roles",
        name: "incidentRoles",
        meta: { title: "Incident Roles" },
        component: { template: "<div />" },
      },
    ],
  })
  await router.push({ name: "incidentRoles", query })
  await router.isReady()

  const wrapper = mount(IncidentRoleTable, {
    global: {
      plugins: [router, vuetify],
      stubs: ["settings-breadcrumbs"],
    },
  })
  await settle()

  // The children fetch once on mount; the assertions are all about the refetch.
  IncidentRoleApi.getRolePolicies.mockClear()
  return { wrapper, router }
}

/** Drive the real v-model wiring rather than assigning to the parent directly. */
async function selectProject(wrapper, project) {
  await wrapper
    .findComponent({ name: "settings-breadcrumbs" })
    .vm.$emit("update:modelValue", [project])
  await settle()
}

const rolesFetchedFor = () => IncidentRoleApi.getRolePolicies.mock.calls

describe("incident roles project selector", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    IncidentRoleApi.getRolePolicies.mockResolvedValue({ data: { policies: [] } })
  })

  it("refetches every role policy for the newly selected project", async () => {
    const { wrapper } = await mountTable()

    await selectProject(wrapper, { id: 7, name: "acme" })

    expect(rolesFetchedFor()).toEqual([
      ["Incident Commander", "acme"],
      ["Liaison", "acme"],
      ["Scribe", "acme"],
    ])
  })

  it("pushes the selected project into the URL", async () => {
    const { wrapper, router } = await mountTable()

    await selectProject(wrapper, { id: 7, name: "acme" })

    expect(router.currentRoute.value.query.project).toBe("acme")
  })

  it("scopes the initial fetch to ?project= in the URL", async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        {
          path: "/settings/incident-roles",
          name: "incidentRoles",
          meta: { title: "Incident Roles" },
          component: { template: "<div />" },
        },
      ],
    })
    await router.push({ name: "incidentRoles", query: { project: "acme" } })
    await router.isReady()

    mount(IncidentRoleTable, {
      global: { plugins: [router, vuetify], stubs: ["settings-breadcrumbs"] },
    })
    await settle()

    expect(rolesFetchedFor()).toEqual([
      ["Incident Commander", "acme"],
      ["Liaison", "acme"],
      ["Scribe", "acme"],
    ])
  })

  it("unscopes rather than throwing when the selector is cleared", async () => {
    // SettingsBreadcrumbs emits `[value]` unconditionally, so clearing the
    // autocomplete stores `[null]` rather than `[]`. Both the watcher and the
    // `project` computed have to survive that, and only the refetch assertion
    // catches the computed: the watcher pushes the URL before the re-render
    // that would throw, so a URL-only assertion passes over the crash.
    const { wrapper, router } = await mountTable({ project: "acme" })

    await selectProject(wrapper, null)

    expect(rolesFetchedFor()).toEqual([
      ["Incident Commander", undefined],
      ["Liaison", undefined],
      ["Scribe", undefined],
    ])
    expect(router.currentRoute.value.query).not.toHaveProperty("project")
  })
})
