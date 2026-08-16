/**
 * Every server-side table's store must normalise `sortBy` before it reaches
 * the API client (#152).
 *
 * Vuetify 3's `toggleSort` replaces `sortBy` wholesale with `[{ key, order }]`.
 * axios serialises that to `sortBy[0][key]=name&sortBy[0][order]=asc`, which
 * the backend reads as no sort at all -- and there is no fallback ORDER BY, so
 * it runs LIMIT/OFFSET unordered and paging repeats or skips rows. Three
 * stores passed `state.table.options` straight through and so never sorted.
 *
 * These call each store's real `getAll` with a stubbed API client and assert on
 * the params it was handed, which is the boundary the bug lived at. A test
 * against `createParametersFromTableOptions` alone would pass while these
 * stores still bypassed it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/project/api", () => ({ default: { getAll: vi.fn() } }))
vi.mock("@/auth/api", () => ({ default: { getAll: vi.fn() } }))
vi.mock("@/prompt/api", () => ({ default: { getAll: vi.fn(), getGenaiTypes: vi.fn() } }))
vi.mock("@/definition/api", () => ({ default: { getAll: vi.fn() } }))
vi.mock("@/api", () => ({ default: { get: vi.fn() } }))
vi.mock("@/router/index", () => ({ default: { push: vi.fn() } }))

import ProjectApi from "@/project/api"
import UserApi from "@/auth/api"
import PromptApi from "@/prompt/api"
import DefinitionApi from "@/definition/api"

import projectStore from "@/project/store"
import authStore from "@/auth/store"
import promptStore from "@/prompt/store"
import definitionStore from "@/definition/store"

const EMPTY_PAGE = { data: { items: [], total: 0 } }

/** What Vuetify 3 leaves in `sortBy` after one click on the "name" header. */
const CLICKED = [{ key: "name", order: "desc" }]

/**
 * Run a store's debounced `getAll` and return the params its API client saw.
 *
 * Each store's `getAll` is wrapped in lodash `debounce`, so the call is only
 * made once the timers are advanced.
 */
async function paramsFrom(store, api, mutate) {
  api.getAll.mockClear()
  api.getAll.mockResolvedValue(EMPTY_PAGE)

  mutate(store.state.table.options)

  store.actions.getAll({
    commit: () => {},
    dispatch: () => Promise.resolve([]),
    state: store.state,
  })
  await vi.runAllTimersAsync()

  expect(api.getAll).toHaveBeenCalledOnce()
  return api.getAll.mock.calls[0][0]
}

const STORES = [
  { name: "project", store: projectStore, api: ProjectApi },
  { name: "auth (organization members)", store: authStore, api: UserApi },
  { name: "prompt", store: promptStore, api: PromptApi },
  { name: "definition", store: definitionStore, api: DefinitionApi },
]

describe("server-table stores normalise sortBy before it reaches the API", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  for (const { name, store, api } of STORES) {
    it(`${name}: a column-header click sends a plain field name`, async () => {
      const params = await paramsFrom(store, api, (options) => {
        options.sortBy = CLICKED
        options.descending = [true]
      })

      expect(params.sortBy).toEqual(["name"])
      expect(params.descending).toEqual([true])
    })

    it(`${name}: an ascending click sends descending false`, async () => {
      const params = await paramsFrom(store, api, (options) => {
        options.sortBy = [{ key: "name", order: "asc" }]
        options.descending = [true]
      })

      expect(params.sortBy).toEqual(["name"])
      expect(params.descending).toEqual([false])
    })

    it(`${name}: the store's own options are not stripped by the call`, async () => {
      // createParametersFromTableOptions deletes `sortBy` off what it is given.
      await paramsFrom(store, api, (options) => {
        options.sortBy = CLICKED
        options.descending = [true]
      })

      expect(store.state.table.options.sortBy).toEqual(CLICKED)
    })
  }
})

describe("definition store defaults", () => {
  it("declares descending as an array, so the default sort is really descending", () => {
    // A bare `true` makes `descending[0]` undefined, silently sorting ascending.
    expect(Array.isArray(definitionStore.state.table.options.descending)).toBe(true)
    expect(definitionStore.state.table.options.descending[0]).toBe(true)
  })
})
