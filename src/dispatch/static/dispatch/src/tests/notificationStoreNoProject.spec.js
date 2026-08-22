/**
 * The Notifications table's refetch must survive no project being selected (#259).
 *
 * Unlike the other 32 settings tables, guarding `Table.vue`'s watcher is not
 * enough here: `notification/store.js`'s own `getAll` reads
 * `filters.project[0].name` to load the project's daily/weekly report settings,
 * and did so before returning `NotificationApi.getAll(params)` -- so the refetch
 * still died one frame later.
 *
 * Asserted at the API-client boundary, the same one `promptProjectFilter.spec.js`
 * uses for this kind of store.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/notification/api", () => ({
  default: { getAll: vi.fn(), get: vi.fn() },
}))
vi.mock("@/project/api", () => ({
  default: { getAll: vi.fn(), update: vi.fn() },
}))

import NotificationApi from "@/notification/api"
import ProjectApi from "@/project/api"
import notificationStore from "@/notification/store"

const EMPTY_PAGE = { data: { items: [], total: 0 } }

/** The store's own declared defaults, restored per call so tests don't leak. */
const baseline = () => ({
  q: "",
  page: 1,
  itemsPerPage: 25,
  sortBy: ["name"],
  descending: [false],
  filters: { project: [] },
})

async function getAllWith(mutate) {
  NotificationApi.getAll.mockClear()
  ProjectApi.getAll.mockClear()
  NotificationApi.getAll.mockResolvedValue(EMPTY_PAGE)
  ProjectApi.getAll.mockResolvedValue(EMPTY_PAGE)

  mutate(Object.assign(notificationStore.state.table.options, baseline()))

  notificationStore.actions.getAll({
    commit: () => {},
    state: notificationStore.state,
  })
  await vi.runAllTimersAsync()
}

describe("notification store getAll with no project selected", () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it("still fetches the notification rows", async () => {
    await getAllWith(() => {})

    expect(NotificationApi.getAll).toHaveBeenCalledOnce()
  })

  it("does not look up a project it was never given", async () => {
    // An undefined `q` matches every project, so the first one's report settings
    // would be shown as if the user had selected it.
    await getAllWith(() => {})

    expect(ProjectApi.getAll).not.toHaveBeenCalled()
  })

  it("still looks the project up once one is selected", async () => {
    await getAllWith((options) => {
      options.filters.project = [{ id: 7, name: "acme" }]
    })

    expect(NotificationApi.getAll).toHaveBeenCalledOnce()
    expect(ProjectApi.getAll).toHaveBeenCalledWith({ q: "acme" })
  })
})
