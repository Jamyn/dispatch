// date-fns 4 throws a TypeError where 2.x returned Invalid Date. `reported_at`
// and `created_at` are nullable columns declared `datetime | None` in every read
// schema, so a record missing one now takes a whole dashboard down instead of
// contributing an Invalid Date. parseISOOrInvalid restores the 2.x result, and
// these tests pin that result -- an "Invalid Date"/NaN group key and a NaN
// duration -- because substituting anything real would move the numbers
// operators read off these charts.
import { expect, test, describe } from "vitest"

import { parseISOOrInvalid } from "@/util/date"
import CaseNewClosedAverageTimeCard from "@/dashboard/case/CaseNewClosedAverageTimeCard.vue"
import CaseNewTriageAverageTimeCard from "@/dashboard/case/CaseNewTriageAverageTimeCard.vue"
import CaseOverview from "@/dashboard/case/CaseOverview.vue"
import IncidentHeatmapCard from "@/dashboard/incident/IncidentHeatmapCard.vue"
import IncidentMeanResponseTimeCard from "@/dashboard/incident/IncidentMeanResponseTimeCard.vue"
import IncidentOverview from "@/dashboard/incident/IncidentOverview.vue"
import IncidentsTab from "@/data/source/IncidentsTab.vue"
import TaskActiveTimeCard from "@/task/TaskActiveTimeCard.vue"
import TaskOverview from "@/dashboard/task/TaskOverview.vue"

const computed = (component, name, self) => component.computed[name].call(self)

describe("parseISOOrInvalid", () => {
  test.each([null, undefined, ""])("returns an Invalid Date for %p", (value) => {
    const parsed = parseISOOrInvalid(value)
    expect(parsed).toBeInstanceOf(Date)
    expect(parsed.getTime()).toBeNaN()
  })

  test("parses a real ISO timestamp unchanged", () => {
    expect(parseISOOrInvalid("2024-01-15T12:00:00Z").toISOString()).toBe("2024-01-15T12:00:00.000Z")
  })
})

// Every card that measures a duration from a nullable timestamp.
describe.each([
  ["CaseNewClosedAverageTimeCard", CaseNewClosedAverageTimeCard, "reported_at", "closed_at"],
  ["CaseNewTriageAverageTimeCard", CaseNewTriageAverageTimeCard, "reported_at", "triage_at"],
  ["IncidentMeanResponseTimeCard", IncidentMeanResponseTimeCard, "reported_at", "stable_at"],
  ["TaskActiveTimeCard", TaskActiveTimeCard, "created_at", "resolved_at"],
])("%s", (_label, component, startField, endField) => {
  const series = (modelValue) => computed(component, "series", { modelValue })

  test("a record with no start timestamp contributes NaN rather than throwing", () => {
    const bucket = [{ status: "Stable", [startField]: null, [endField]: "2024-01-15T12:00:00Z" }]
    expect(() => series({ Jan: bucket })).not.toThrow()
    expect(series({ Jan: bucket })[0].data[0]).toBeNaN()
  })

  test("a fully populated record still averages to real hours", () => {
    const bucket = [
      {
        status: "Stable",
        [startField]: "2024-01-15T00:00:00Z",
        [endField]: "2024-01-15T10:00:00Z",
      },
      {
        status: "Stable",
        [startField]: "2024-01-15T00:00:00Z",
        [endField]: "2024-01-15T20:00:00Z",
      },
    ]
    expect(series({ Jan: bucket })[0].data[0]).toBe(15)
  })
})

// Every dashboard that groups or sums over a nullable timestamp.
describe.each([
  ["IncidentOverview", IncidentOverview, "incidentsBy", "totalResponseHours", "reported_at"],
  ["CaseOverview", CaseOverview, "casesBy", "totalHours", "reported_at"],
])("%s", (_label, component, prefix, totalName, field) => {
  const items = [{ [field]: null, incidents: [] }]

  test("groups an unset timestamp under the Invalid Date buckets", () => {
    const byYear = computed(component, `${prefix}Year`, { items })
    expect(Object.keys(byYear)).toEqual(["NaN"])

    const byMonth = computed(component, `${prefix}Month`, { items, [`${prefix}Year`]: byYear })
    expect(Object.keys(byMonth)).toEqual(["Invalid Date"])

    expect(Object.keys(computed(component, `${prefix}Quarter`, { items }))).toEqual(["QNaN"])
  })

  test("sums an unset timestamp to NaN rather than throwing", () => {
    expect(() => computed(component, totalName, { items })).not.toThrow()
    expect(computed(component, totalName, { items })).toBeNaN()
  })

  test("a populated timestamp still groups by its real month", () => {
    const real = [{ [field]: "2024-03-15T12:00:00Z", incidents: [] }]
    const byYear = computed(component, `${prefix}Year`, { items: real })
    expect(
      Object.keys(
        computed(component, `${prefix}Month`, { items: real, [`${prefix}Year`]: byYear }),
      ),
    ).toEqual(["Mar"])
  })
})

test("TaskOverview groups and sums an unset created_at without throwing", () => {
  const items = [{ created_at: null, assignees: [] }]
  expect(Object.keys(computed(TaskOverview, "tasksByMonth", { items }))).toEqual(["Invalid Date"])
  expect(computed(TaskOverview, "totalHours", { items })).toBeNaN()
  expect(
    Object.keys(
      computed(TaskOverview, "tasksByMonth", { items: [{ created_at: "2024-03-15T12:00:00Z" }] }),
    ),
  ).toEqual(["Mar"])
})

test("IncidentHeatmapCard buckets an unset reported_at without throwing", () => {
  const self = {
    modelValue: { Jan: [{ reported_at: null }] },
    weekdays: computed(IncidentHeatmapCard, "weekdays", {}),
  }
  let series
  expect(() => {
    series = computed(IncidentHeatmapCard, "series", self)
  }).not.toThrow()
  expect(series.find((s) => s.name === "Invalid Date").data[0].y).toBe(1)
})

test("IncidentsTab groups an unset reported_at without throwing", () => {
  const incidents = [{ reported_at: null }]
  expect(() => computed(IncidentsTab, "incidentsByMonth", { incidents })).not.toThrow()
  expect(Object.keys(computed(IncidentsTab, "incidentsByMonth", { incidents }))).toEqual([
    "Invalid Date",
  ])
})
