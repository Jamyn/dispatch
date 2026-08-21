/**
 * The Prompts table's project scoper must reach the query (#170).
 *
 * `Table.vue` binds the settings scoper to `table.options.filters.project` and
 * refetches when it changes, but the store handed
 * `createParametersFromTableOptions` a hard-coded `filters: {}`, so every
 * refetch returned the same unscoped list. `Prompt` has `ProjectMixin` and its
 * route is the generic `search_filter_sort_paginate`, so the backend applies
 * the filter as soon as one is sent.
 *
 * These assert on the params the API client is handed -- the boundary the bug
 * lived at, and the one `tableSortParams.spec.js` already uses for this store.
 *
 * The scoper reaches the store in two shapes, so both are pinned: picking from
 * the menu stores a whole project record (ProjectMenuSelect sets `return-object`)
 * and `createFilterExpression` keys that on `id`, while `Table.vue`'s route-query
 * bootstrap stores a bare `{ name }`, which keys on `name`.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/prompt/api", () => ({
  default: { getAll: vi.fn(), getDefaults: vi.fn() },
}))
vi.mock("@/api", () => ({ default: { get: vi.fn() } }))

import PromptApi from "@/prompt/api"
import promptStore from "@/prompt/store"

const EMPTY_PAGE = { data: { items: [], total: 0 } }

/** The store's own declared defaults, restored per call so tests don't leak. */
const baseline = () => ({
  q: "",
  page: 1,
  itemsPerPage: 25,
  sortBy: ["genai_type"],
  descending: [false],
  filters: { project: [] },
})

/**
 * Run the store's debounced `getAll` and return the params PromptApi saw.
 * The call is only made once the timers are advanced.
 */
async function paramsFrom(mutate) {
  PromptApi.getAll.mockClear()
  PromptApi.getAll.mockResolvedValue(EMPTY_PAGE)

  mutate(Object.assign(promptStore.state.table.options, baseline()))

  promptStore.actions.getAll({
    commit: () => {},
    dispatch: () => Promise.resolve([]),
    state: promptStore.state,
  })
  await vi.runAllTimersAsync()

  expect(PromptApi.getAll).toHaveBeenCalledOnce()
  return PromptApi.getAll.mock.calls[0][0]
}

describe("prompt store sends the project scoper to the backend", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("a project picked from the menu reaches the request as a Project id filter", async () => {
    const params = await paramsFrom((options) => {
      options.filters.project = [{ id: 7, name: "acme" }]
    })

    expect(JSON.parse(params.filter)).toEqual({
      and: [{ or: [{ model: "Project", field: "id", op: "==", value: 7 }] }],
    })
  })

  it("a project restored from the route query reaches it as a Project name filter", async () => {
    const params = await paramsFrom((options) => {
      options.filters.project = [{ name: "acme" }]
    })

    expect(JSON.parse(params.filter)).toEqual({
      and: [{ or: [{ model: "Project", field: "name", op: "==", value: "acme" }] }],
    })
  })

  it("no selected project still sends no filter", async () => {
    const params = await paramsFrom(() => {})

    expect(params.filter).toBeUndefined()
  })

  it("scoping by project preserves sorting and pagination", async () => {
    const params = await paramsFrom((options) => {
      options.filters.project = [{ name: "acme" }]
      options.sortBy = [{ key: "genai_type", order: "desc" }]
      options.descending = [false]
      options.page = 3
      options.itemsPerPage = 50
    })

    expect(params.sortBy).toEqual(["genai_type"])
    expect(params.descending).toEqual([true])
    expect(params.page).toBe(3)
    expect(params.itemsPerPage).toBe(50)
    expect(JSON.parse(params.filter).and[0].or[0].value).toBe("acme")
  })

  it("does not strip the scoper off the store's own options", async () => {
    // createParametersFromTableOptions deletes `filters` from what it is given,
    // so passing state.table.options itself would clear the user's selection.
    await paramsFrom((options) => {
      options.filters.project = [{ name: "acme" }]
    })

    expect(promptStore.state.table.options.filters.project).toEqual([{ name: "acme" }])
  })
})
