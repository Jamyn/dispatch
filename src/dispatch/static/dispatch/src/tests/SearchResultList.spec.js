import { mount } from "@vue/test-utils"
import { createStore } from "vuex"
import { createVuetify } from "vuetify"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"
import { describe, expect, it, vi } from "vitest"
import ResultList from "@/search/ResultList.vue"
import QuerySummaryTable from "@/data/query/QuerySummaryTable.vue"
import SourceSummaryTable from "@/data/source/SourceSummaryTable.vue"

const vuetify = createVuetify({ components, directives })

const project = { display_name: "default", color: "#1976d2" }

// Every panel gets its own array, so a panel wired to the wrong result set
// shows up as the wrong rows rather than as a coincidental match.
const emptyResults = {
  incidents: [],
  cases: [],
  tasks: [],
  sources: [],
  queries: [],
  documents: [],
  tags: [],
}

const aSource = { id: 1, name: "source-one", description: "a source", project }
const aQuery = { id: 2, name: "query-one", description: "a query", language: "sql", project }

function mountResultList(results) {
  const store = createStore({
    modules: {
      search: {
        namespaced: true,
        state: { results, query: "one", loading: false },
        actions: { setQuery: vi.fn(), getResults: vi.fn() },
      },
    },
  })

  return mount(ResultList, {
    global: {
      plugins: [store, vuetify],
      mocks: { $route: { query: { q: "one" } } },
    },
  })
}

// Panel bodies are lazy: nothing below a title is mounted until it is opened.
async function openPanel(wrapper, label) {
  const title = wrapper.findAll(".v-expansion-panel-title").find((t) => t.text().startsWith(label))
  expect(title, `no "${label}" panel`).toBeTruthy()
  await title.trigger("click")
  return title
}

describe("SearchResultList", () => {
  it("renders queries, not sources, in the Queries panel", async () => {
    const wrapper = mountResultList({ ...emptyResults, sources: [aSource], queries: [aQuery] })

    await openPanel(wrapper, "Queries")

    expect(wrapper.findComponent(QuerySummaryTable).props("items")).toEqual([aQuery])
  })

  it("shows no rows under Queries (0) when only sources matched", async () => {
    const wrapper = mountResultList({ ...emptyResults, sources: [aSource] })

    const title = await openPanel(wrapper, "Queries")

    expect(title.text()).toBe("Queries (0)")
    expect(wrapper.findComponent(QuerySummaryTable).props("items")).toEqual([])
  })

  it("still renders sources in the Sources panel", async () => {
    const wrapper = mountResultList({ ...emptyResults, sources: [aSource], queries: [aQuery] })

    const title = await openPanel(wrapper, "Sources")

    expect(title.text()).toBe("Sources (1)")
    expect(wrapper.findComponent(SourceSummaryTable).props("items")).toEqual([aSource])
  })
})
