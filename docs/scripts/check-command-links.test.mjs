import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { test } from "node:test"
import { fileURLToPath } from "node:url"
import path from "node:path"

import { checkCommandLinks } from "./check-command-links.mjs"

const here = path.dirname(fileURLToPath(import.meta.url))
const commanderPage = path.join(here, "..", "docs/user-guide/incidents/commander.mdx")

/** A minimal page with the same shape as the Incident Commander page. */
function page(entries, sections) {
  return [
    "# Commander",
    "",
    "## All Slack commands",
    "",
    ...entries,
    "",
    "## People",
    "",
    ...sections.flatMap((s) => [`### ${s}`, "", "Description.", ""]),
  ].join("\n")
}

const only = (source) => {
  const problems = checkCommandLinks(source)
  assert.equal(problems.length, 1, `expected exactly one problem, got ${problems.length}`)
  return problems[0]
}

test("the real Incident Commander page is consistent", async () => {
  assert.deepEqual(checkCommandLinks(await readFile(commanderPage, "utf8")), [])
})

test("a linked command with a matching section passes", () => {
  assert.deepEqual(
    checkCommandLinks(page(["- [`/dispatch-summary`](#dispatch-summary)"], ["/dispatch-summary"])),
    []
  )
})

// The regression this exists for: issue #82 shipped three commands as plain
// code spans, which MDX renders happily and the docs build never questions.
test("a command listed without a link fails", () => {
  assert.match(only(page(["- `/dispatch-list-signals`"], [])), /listed but not linked/)
})

// CommonMark accepts -, * and + interchangeably. Recognizing only "- " let the
// exact #82 defect through with a green gate.
for (const marker of ["-", "*", "+"]) {
  test(`an unlinked command using the "${marker}" bullet marker fails`, () => {
    assert.match(only(page([`${marker} \`/dispatch-summary\``], [])), /listed but not linked/)
  })
}

test("a tab after the bullet marker is still recognized", () => {
  assert.match(only(page(["-\t`/dispatch-summary`"], [])), /listed but not linked/)
})

test("a linked command with no section fails", () => {
  assert.deepEqual(checkCommandLinks(page(["- [`/dispatch-summary`](#dispatch-summary)"], [])), [
    'line 5: `/dispatch-summary` links to #dispatch-summary but has no "### /dispatch-summary" section',
  ])
})

test("a link pointing at the wrong anchor fails", () => {
  assert.match(
    only(page(["- [`/dispatch-summary`](#dispatch-sumary)"], ["/dispatch-summary"])),
    /links to #dispatch-sumary, expected #dispatch-summary/
  )
})

test("a section missing from the list fails", () => {
  assert.match(only(page([], ["/dispatch-summary"])), /is not in the All Slack commands list/)
})

test("a command listed twice fails", () => {
  const source = page(
    ["- [`/dispatch-summary`](#dispatch-summary)", "- [`/dispatch-summary`](#dispatch-summary)"],
    ["/dispatch-summary"]
  )
  assert.match(only(source), /is listed twice/)
})

// Docusaurus lowercases generated heading ids, and matches the link fragment
// against them case-sensitively -- so must this.
test("a heading is expected to resolve to its lowercased anchor", () => {
  assert.deepEqual(
    checkCommandLinks(page(["- [`/dispatch-Summary`](#dispatch-summary)"], ["/dispatch-Summary"])),
    []
  )
})

test("a link matching the heading's case rather than the generated id fails", () => {
  assert.match(
    only(page(["- [`/dispatch-Summary`](#dispatch-Summary)"], ["/dispatch-Summary"])),
    /expected #dispatch-summary/
  )
})

// `npm run write-heading-ids` rewrites every heading this way; the checker has
// to keep working afterwards rather than reporting the whole page as missing.
test("an explicit heading id is honoured", () => {
  const source = [
    "## All Slack commands {#all-slack-commands}",
    "",
    "- [`/dispatch-summary`](#read-in-summary)",
    "",
    "## People",
    "",
    "### /dispatch-summary {#read-in-summary}",
    "",
    "Description.",
  ].join("\n")
  assert.deepEqual(checkCommandLinks(source), [])
})

test("an explicit heading id makes the default anchor wrong", () => {
  const source = [
    "## All Slack commands",
    "",
    "- [`/dispatch-summary`](#dispatch-summary)",
    "",
    "## People",
    "",
    "### /dispatch-summary {#read-in-summary}",
  ].join("\n")
  assert.match(only(source), /expected #read-in-summary/)
})

// Anything in the list that is not a uniform entry is an error rather than a
// skipped line, because a skipped line is how #82 stayed invisible.
for (const [name, entry] of [
  ["trailing prose", "- [`/dispatch-summary`](#dispatch-summary) — AI generated"],
  ["a reference-style link", "- [`/dispatch-summary`][summary]"],
  ["a nested sub-bullet", "  - only in incident channels"],
]) {
  test(`${name} in the command list is reported, not skipped`, () => {
    const problems = checkCommandLinks(page([entry], []))
    assert.ok(
      problems.some((p) => /must be `- \[`\/command`\]\(#anchor\)`/.test(p)),
      `expected a shape complaint, got ${JSON.stringify(problems)}`
    )
  })
}

test("blank lines and comments in the command list are allowed", () => {
  const source = page(
    [
      "- [`/dispatch-summary`](#dispatch-summary)",
      "",
      "{/* a note to editors */}",
      "<!-- another note -->",
    ],
    ["/dispatch-summary"]
  )
  assert.deepEqual(checkCommandLinks(source), [])
})

// A single-line regex cannot see these, which is what CodeQL's js/bad-tag-filter
// caught: the entries would have been reported as malformed list lines.
test("a comment spanning several lines in the command list is allowed", () => {
  const source = page(
    [
      "- [`/dispatch-summary`](#dispatch-summary)",
      "{/* a note to editors",
      "   continued onto a second line */}",
      "<!-- and another",
      "     one -->",
    ],
    ["/dispatch-summary"]
  )
  assert.deepEqual(checkCommandLinks(source), [])
})

test("a commented-out section does not count as a section", () => {
  const source = [
    "## All Slack commands",
    "",
    "- [`/dispatch-summary`](#dispatch-summary)",
    "",
    "## People",
    "",
    "{/*",
    "### /dispatch-summary",
    "*/}",
  ].join("\n")
  assert.match(only(source), /has no "### \/dispatch-summary" section/)
})

test("fenced code blocks are not parsed as entries or sections", () => {
  const source = [
    "## All Slack commands",
    "",
    "- [`/dispatch-summary`](#dispatch-summary)",
    "",
    "```markdown",
    "- `/dispatch-not-a-real-command`",
    "### /dispatch-also-not-real",
    "```",
    "",
    "## People",
    "",
    "### /dispatch-summary",
    "",
    "Description.",
  ].join("\n")
  assert.deepEqual(checkCommandLinks(source), [])
})

test("a ``` inside a ~~~ fence does not flip fence tracking", () => {
  const source = [
    "## All Slack commands",
    "",
    "- [`/dispatch-summary`](#dispatch-summary)",
    "",
    "~~~markdown",
    "```",
    "- `/dispatch-not-a-real-command`",
    "~~~",
    "",
    "## People",
    "",
    "### /dispatch-summary",
    "",
    "Description.",
  ].join("\n")
  assert.deepEqual(checkCommandLinks(source), [])
})

test("a page without the command list is reported, not silently skipped", () => {
  assert.deepEqual(checkCommandLinks("# Some other page\n"), [
    'no "## All Slack commands" heading found',
  ])
})
