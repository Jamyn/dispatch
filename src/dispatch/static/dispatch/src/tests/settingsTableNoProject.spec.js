/**
 * A settings table must still refetch when no project is selected (#259).
 *
 * Every settings `Table.vue` watches its filter controls and pushes the current
 * project into the URL before calling `getAll()`, indexing `[0].name`
 * unconditionally. The throw lands on the line before `getAll()`, so the refetch
 * never runs and the search box goes silently dead rather than reporting an error.
 *
 * `filters.project` empties two ways, and both are pinned below:
 *   - it starts `[]`, on the tables that guard the route-query seeding with
 *     `if (this.$route.query.project)` -- `term` and `prompt`;
 *   - it becomes `[null]` on any of them once the breadcrumb selector is cleared,
 *     since `SettingsBreadcrumbs` emits `[value]` unconditionally.
 * The other 31 tables seed `[{ name: undefined }]` instead, which never threw --
 * the shared watcher is still guarded uniformly, because the `[null]` path is not.
 *
 * `term/Table.vue` is the representative: all 33 carry the byte-identical
 * watcher, and this one has no extra work in `created()` to mount around.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"
import { mount, flushPromises } from "@vue/test-utils"
import { createStore } from "vuex"
import { createRouter, createMemoryHistory } from "vue-router"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"

vi.mock("@/term/api", () => ({
  default: { getAll: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() },
}))

import termStore from "@/term/store"
import TermTable from "@/term/Table.vue"

const vuetify = createVuetify({ components, directives })

/** The store's declared table defaults, restored per mount so tests don't leak. */
const tableDefaults = () => ({
  q: "",
  page: 1,
  itemsPerPage: 25,
  sortBy: ["text"],
  descending: [false],
  filters: { project: [] },
})

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
  const getAll = vi.fn()

  const store = createStore({
    modules: {
      term: {
        namespaced: true,
        state: Object.assign(termStore.state, {
          table: {
            ...termStore.state.table,
            options: tableDefaults(),
            rows: { items: [], total: 0 },
            loading: false,
          },
        }),
        getters: termStore.getters,
        mutations: termStore.mutations,
        actions: { ...termStore.actions, getAll, createEditShow: vi.fn(), removeShow: vi.fn() },
      },
    },
  })

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/settings/terms", name: "terms", component: { template: "<div />" } }],
  })
  await router.push({ name: "terms", query })
  await router.isReady()

  const wrapper = mount(TermTable, {
    global: {
      plugins: [store, router, vuetify],
      stubs: ["new-edit-sheet", "delete-dialog", "settings-breadcrumbs"],
    },
  })
  await settle()

  // created() fetches once on mount; the assertions are all about the refetch.
  getAll.mockClear()
  return { wrapper, router, getAll }
}

describe("settings table with no project selected", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("refetches when the search box changes", async () => {
    const { wrapper, getAll } = await mountTable()
    expect(wrapper.vm.project).toEqual([])

    wrapper.vm.q = "denial"
    await settle()

    expect(getAll).toHaveBeenCalledOnce()
  })

  it("leaves the project out of the URL rather than throwing", async () => {
    const { wrapper, router, getAll } = await mountTable()

    wrapper.vm.q = "denial"
    await settle()

    // Without the refetch assertion this passes on the bug too: a watcher that
    // throws never reaches `$router.push`, so the URL is equally untouched.
    expect(getAll).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.query).not.toHaveProperty("project")
    expect(router.currentRoute.value.fullPath).toBe("/settings/terms")
  })

  it("refetches after the breadcrumb selector is cleared", async () => {
    // SettingsBreadcrumbs emits `[value]`, so clearing the autocomplete stores
    // `[null]` rather than `[]`. This is the path the other 31 tables reach.
    const { wrapper, router, getAll } = await mountTable({ project: "acme" })

    wrapper.vm.project = [null]
    await settle()

    expect(getAll).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.query).not.toHaveProperty("project")
  })

  it("refetches when sorting changes", async () => {
    const { wrapper, getAll } = await mountTable()

    wrapper.vm.sortBy = [{ key: "text", order: "desc" }]
    await settle()

    expect(getAll).toHaveBeenCalledOnce()
  })

  it("refetches when the page size changes", async () => {
    const { wrapper, getAll } = await mountTable()

    wrapper.vm.itemsPerPage = 50
    await settle()

    expect(getAll).toHaveBeenCalledOnce()
  })

  it("still scopes the URL to a project once one is selected", async () => {
    const { wrapper, router, getAll } = await mountTable()

    wrapper.vm.project = [{ id: 7, name: "acme" }]
    await settle()

    expect(getAll).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.query.project).toBe("acme")
  })

  it("keeps scoping a table opened with ?project= in the URL", async () => {
    const { wrapper, router, getAll } = await mountTable({ project: "acme" })
    expect(wrapper.vm.project).toEqual([{ name: "acme" }])

    wrapper.vm.q = "denial"
    await settle()

    expect(getAll).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.query.project).toBe("acme")
  })
})
