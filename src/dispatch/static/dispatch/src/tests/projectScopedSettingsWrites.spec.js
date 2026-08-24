/**
 * Project-scoped settings writes must be a no-op with no project selected (#288).
 *
 * `filters.project` defaults to `[]`, the settings tables seed
 * `[{ name: undefined }]` when the URL carries no `?project=`, and clearing the
 * breadcrumb selector stores `[null]`. None of those name a project, and
 * `ProjectApi.getAll({ q: undefined })` matches every project -- so an unguarded
 * write either throws or saves the setting onto whichever project sorts first.
 *
 * Asserted at the API-client boundary, like `notificationStoreNoProject.spec.js`.
 */

import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/notification/api", () => ({
  default: { getAll: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() },
}))
vi.mock("@/incident/priority/api", () => ({
  default: { getAll: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() },
}))
vi.mock("@/project/api", () => ({
  default: { getAll: vi.fn(), get: vi.fn(), update: vi.fn() },
}))

/** Every filter shape a settings page can reach without the user picking a project. */
const NO_PROJECT = [
  ["the store default", []],
  ["a cleared breadcrumb selector", [null]],
  ["no ?project= in the URL", [{ name: undefined }]],
  // An empty `q` matches every project just as an undefined one does.
  ["an empty ?project=", [{ name: "" }]],
]

const SELECTED = { id: 7, name: "acme" }

let ProjectApi
let notificationStore
let incidentPriorityStore

// Fresh modules per test: `incident/priority/store` keeps the stable-priority
// debounce gate in a module-level variable that would otherwise leak between tests.
beforeEach(async () => {
  vi.resetModules()
  // `vi.mock` factories are evaluated once per file, so the mock functions
  // survive resetModules and would carry calls between tests.
  vi.clearAllMocks()
  ProjectApi = (await import("@/project/api")).default
  ProjectApi.getAll.mockResolvedValue({ data: { items: [{ ...SELECTED }], total: 1 } })
  ProjectApi.update.mockResolvedValue({ data: { ...SELECTED } })
  notificationStore = (await import("@/notification/store")).default
  incidentPriorityStore = (await import("@/incident/priority/store")).default
})

const noop = () => {}

function callNotificationAction(name, project, value) {
  notificationStore.state.table.options.filters.project = project
  notificationStore.actions[name]({ commit: noop }, value)
}

/**
 * `commitStablePriority` is only reachable through `updateStablePriority`, which
 * ignores the first call while it primes `oldStablePriority`.
 */
function selectStablePriority(project) {
  incidentPriorityStore.state.table.options.filters.project = project
  incidentPriorityStore.state.stablePriority = null
  incidentPriorityStore.actions.updateStablePriority({ commit: noop }, false)
  incidentPriorityStore.state.stablePriority = { id: 3, name: "Stable" }
  incidentPriorityStore.actions.updateStablePriority({ commit: noop }, true)
}

describe("notification store project-scoped writes", () => {
  const actions = ["updateDailyReports", "updateWeeklyReports", "updateWeeklyReportNotificationId"]

  for (const action of actions) {
    for (const [label, project] of NO_PROJECT) {
      it(`${action} is a no-op with ${label}`, () => {
        expect(() => callNotificationAction(action, project, true)).not.toThrow()
        expect(ProjectApi.getAll).not.toHaveBeenCalled()
        expect(ProjectApi.update).not.toHaveBeenCalled()
      })
    }
  }

  it("updateDailyReports still saves once a project is selected", async () => {
    callNotificationAction("updateDailyReports", [{ ...SELECTED }], false)

    expect(ProjectApi.getAll).toHaveBeenCalledWith({ q: "acme" })
    await vi.waitFor(() => expect(ProjectApi.update).toHaveBeenCalledOnce())
    expect(ProjectApi.update).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ send_daily_reports: false }),
    )
  })

  it("updateWeeklyReports still saves once a project is selected", async () => {
    callNotificationAction("updateWeeklyReports", [{ ...SELECTED }], true)

    expect(ProjectApi.getAll).toHaveBeenCalledWith({ q: "acme" })
    await vi.waitFor(() => expect(ProjectApi.update).toHaveBeenCalledOnce())
    expect(ProjectApi.update).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ send_weekly_reports: true }),
    )
  })

  it("updateWeeklyReportNotificationId still saves once a project is selected", async () => {
    callNotificationAction("updateWeeklyReportNotificationId", [{ ...SELECTED }], 42)

    expect(ProjectApi.getAll).toHaveBeenCalledWith({ q: "acme" })
    await vi.waitFor(() => expect(ProjectApi.update).toHaveBeenCalledOnce())
    expect(ProjectApi.update).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ weekly_report_notification_id: 42 }),
    )
  })
})

describe("incident priority store stable-priority write", () => {
  for (const [label, project] of NO_PROJECT) {
    it(`updateStablePriority is a no-op with ${label}`, () => {
      expect(() => selectStablePriority(project)).not.toThrow()
      expect(ProjectApi.getAll).not.toHaveBeenCalled()
      expect(ProjectApi.update).not.toHaveBeenCalled()
    })
  }

  it("still saves once a project is selected", async () => {
    selectStablePriority([{ ...SELECTED }])

    expect(ProjectApi.getAll).toHaveBeenCalledWith({ q: "acme" })
    await vi.waitFor(() => expect(ProjectApi.update).toHaveBeenCalledOnce())
    expect(ProjectApi.update).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ stable_priority_id: 3 }),
    )
  })
})
