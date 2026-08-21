// Guards the locale import in IncidentHeatmapCard across the date-fns 2 -> 4
// upgrade. v2 exposed locales under `date-fns/esm/locale/*`; v3 removed the
// `esm/` subpath entirely, so the old import resolves at dev time but fails the
// production build. These labels are also the sort key for the heatmap series,
// so getting them wrong reorders the chart rather than just relabelling it.
import { expect, test } from "vitest"
import IncidentHeatmapCard from "@/dashboard/incident/IncidentHeatmapCard.vue"

test("weekday labels are the abbreviated en-US day names, Sunday first", () => {
  expect(IncidentHeatmapCard.computed.weekdays.call({})).toEqual([
    "Sun",
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
  ])
})
