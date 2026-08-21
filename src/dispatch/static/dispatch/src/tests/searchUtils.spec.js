import { expect, describe, it } from "vitest"

import SearchUtils from "@/search/utils"

describe("SearchUtils.createSortExpression", () => {
  it("derives key/descending from a store's static string sortBy + descending arrays", () => {
    // e.g. incident/store.js default state: sortBy: ["reported_at"], descending: [true]
    const [sortBy, descending] = SearchUtils.createSortExpression(["reported_at"], [true])
    expect(sortBy).toEqual(["reported_at"])
    expect(descending).toEqual([true])
  })

  it("derives key/descending from Vuetify 3's { key, order } sortBy shape", () => {
    // v-data-table(-server) emits this via `update:sort-by` when a user clicks a
    // column header; it never fires `update:sort-desc`, so a stale `descending`
    // value must NOT override what `order` says.
    const [sortBy, descending] = SearchUtils.createSortExpression(
      [{ key: "name", order: "asc" }],
      [true],
    )
    expect(sortBy).toEqual(["name"])
    expect(descending).toEqual([false])
  })

  it("reads descending correctly for a desc column click", () => {
    const [sortBy, descending] = SearchUtils.createSortExpression(
      [{ key: "reported_at", order: "desc" }],
      [false],
    )
    expect(sortBy).toEqual(["reported_at"])
    expect(descending).toEqual([true])
  })

  it("handles an empty sortBy", () => {
    const [sortBy, descending] = SearchUtils.createSortExpression([], [])
    expect(sortBy).toEqual([])
    expect(descending).toEqual([])
  })
})

describe("SearchUtils.createParametersFromTableOptions", () => {
  it("sends the field name and direction the backend expects after a real column-header click", () => {
    const options = {
      page: 1,
      itemsPerPage: 25,
      sortBy: [{ key: "name", order: "desc" }],
      descending: [true],
      filters: {},
    }
    const params = SearchUtils.createParametersFromTableOptions(options, "Incident")
    expect(params.sortBy).toEqual(["name"])
    expect(params.descending).toEqual([true])
  })
})
