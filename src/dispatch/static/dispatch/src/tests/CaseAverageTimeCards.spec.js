// date-fns 4 throws a TypeError where 2.x returned Invalid Date, so a record
// that never reached the stage a card measures from would take the whole Case
// dashboard down instead of contributing a NaN to its average. The NaN is
// preserved deliberately: it is what these cards already produced for such
// records, and changing it would silently move the numbers operators read.
import { expect, test, describe } from "vitest"
import CaseEscalatedClosedAverageTimeCard from "@/dashboard/case/CaseEscalatedClosedAverageTimeCard.vue"
import CaseTriageEscalatedAverageTimeCard from "@/dashboard/case/CaseTriageEscalatedAverageTimeCard.vue"

const series = (component, modelValue) => component.computed.series.call({ modelValue })

describe.each([
  ["escalated to closed", CaseEscalatedClosedAverageTimeCard, "escalated_at", "closed_at"],
  ["triage to escalated", CaseTriageEscalatedAverageTimeCard, "triage_at", "escalated_at"],
])("%s", (_label, component, startField, endField) => {
  test("a record that never reached the start stage does not throw", () => {
    const bucket = [{ [startField]: null, [endField]: "2024-01-15T12:00:00Z" }]
    expect(() => series(component, { Jan: bucket })).not.toThrow()
    expect(series(component, { Jan: bucket })[0].data[0]).toBeNaN()
  })

  test("a fully populated record still averages to real hours", () => {
    const bucket = [
      { [startField]: "2024-01-15T00:00:00Z", [endField]: "2024-01-15T10:00:00Z" },
      { [startField]: "2024-01-15T00:00:00Z", [endField]: "2024-01-15T20:00:00Z" },
    ]
    expect(series(component, { Jan: bucket })[0].data[0]).toBe(15)
  })
})
