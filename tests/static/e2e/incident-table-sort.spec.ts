import { test, expect } from "./fixtures/dispatch-fixtures"
import register from "./utils/register"

test.describe("Authenticated Dispatch App", () => {
  ;(test.beforeEach(async ({ authPage }) => {
    await register(authPage)
  }),
    test("Clicking a sortable column header sends a well-formed sort request and reorders rows", async ({
      page,
      incidentsPage,
    }) => {
      const seen: string[] = []
      page.on("request", (req) => {
        if (req.url().includes("/api/v1/default/incidents?") && req.method() === "GET") {
          seen.push(req.url())
        }
      })

      await incidentsPage.goto()
      await page.waitForLoadState("networkidle")

      const nameHeader = page.getByRole("columnheader", { name: "Name" })
      await expect(nameHeader).toBeVisible()

      // Name is the second column (first is the select-row checkbox).
      const nameCells = page.locator("tbody tr td:nth-child(2)")

      const beforeCount = seen.length
      await nameHeader.click()
      await expect.poll(() => seen.length, { timeout: 5000 }).toBeGreaterThan(beforeCount)

      const url = new URL(seen[seen.length - 1])
      // axios serializes array-valued params with a "[]" suffix.
      const sortByValues = url.searchParams.getAll("sortBy[]")
      const descendingValues = url.searchParams.getAll("descending[]")

      // The regression this guards against: Vuetify 3 emits sortBy as
      // [{ key, order }], and the old, Vuetify-2-shaped serialization sent
      // that object straight through instead of pulling out a plain field
      // name — the request must carry a real field name, not "[object
      // Object]" or an indexed object param like "sortBy[0][key]".
      expect(sortByValues.length).toBeGreaterThan(0)
      for (const value of sortByValues) {
        expect(value).not.toContain("object Object")
        expect(value).not.toMatch(/\[\d+\]\[key\]/)
      }
      expect(descendingValues.length).toBeGreaterThan(0)
      for (const value of descendingValues) {
        expect(["true", "false"]).toContain(value)
      }

      // Deterministic, coincidence-proof oracle: the request itself says
      // which direction was applied, so assert the rendered Name column
      // actually ended up in that order — a fragile "did the top row's
      // text change" check can pass or fail by luck on a small seeded
      // dataset, this can't.
      const descending = descendingValues[0] === "true"
      await expect
        .poll(async () => {
          const names = await nameCells.allInnerTexts()
          const sorted = [...names].sort((a, b) =>
            descending ? b.localeCompare(a) : a.localeCompare(b),
          )
          return names.length > 1 ? JSON.stringify(names) === JSON.stringify(sorted) : true
        })
        .toBe(true)
    }))
})
