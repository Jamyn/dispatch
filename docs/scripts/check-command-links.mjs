#!/usr/bin/env node
// Keeps the "All Slack commands" list on the Incident Commander page in sync
// with the per-command sections below it.
//
// Docusaurus' onBrokenAnchors setting already fails the build on a link whose
// anchor does not resolve, so this deliberately does not reimplement heading
// slugging. It guards the failure the build cannot see: a command listed as a
// plain code span with no link at all renders without complaint.
//
// This is a line parser rather than a remark plugin so the checked function
// stays a pure, dependency-free unit that node:test can exercise directly. The
// trade is that it understands less MDX than the real parser, so the command
// list is held to one uniform shape and anything else in it is an error --
// failing loudly on an unfamiliar line rather than skipping it. A skipped line
// is how this class of bug hides in the first place.

import { readFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import path from "node:path"

const DEFAULT_TARGET = "docs/user-guide/incidents/commander.mdx"

// The pages a registered command has to appear on, and the source of truth it
// is checked against. Relative to the repository root, not this directory.
const CONFIG_SOURCE = "src/dispatch/plugins/dispatch_slack/config.py"
const DOCUMENTED_IN = [
  "docs/docs/user-guide/incidents/commander.mdx",
  "docs/docs/administration/settings/plugins/configuring-slack.mdx",
]

const LIST_HEADING = "All Slack commands"

// Entries are `- [`/cmd`](#anchor)`; sections are `### /cmd`. CommonMark allows
// -, * and + as bullet markers, and MDX renders all three identically.
const LINKED_ENTRY = /^\s*[-*+][ \t]+\[`(\/[^`]+)`\]\(([^)]*)\)\s*$/
const BARE_ENTRY = /^\s*[-*+][ \t]+`(\/[^`]+)`\s*$/
const HEADING = /^(#{1,6})[ \t]+(.*?)[ \t]*$/
const EXPLICIT_ID = /[ \t]*\{#([^}\s]+)\}$/
const FENCE = /^\s*(`{3,}|~{3,})/

const COMMENTS = [
  ["<!--", "-->"],
  ["{/*", "*/}"],
]

/**
 * Blanks out comment spans so a commented-out heading or list entry is not
 * read as a real one. Tracked across lines rather than matched by a
 * single-line regex, because both comment syntaxes may span newlines.
 */
function stripComments(lines, inFence) {
  let closing = null
  return lines.map((line, i) => {
    if (inFence[i]) return line
    let rest = line
    let kept = ""
    while (rest !== "") {
      if (closing) {
        const end = rest.indexOf(closing)
        if (end === -1) break
        rest = rest.slice(end + closing.length)
        closing = null
        continue
      }
      const next = COMMENTS.map(([open, close]) => [rest.indexOf(open), open, close])
        .filter(([at]) => at !== -1)
        .sort((a, b) => a[0] - b[0])[0]
      if (!next) {
        kept += rest
        break
      }
      const [at, open, close] = next
      kept += rest.slice(0, at)
      rest = rest.slice(at + open.length)
      closing = close
    }
    return kept
  })
}

/** Splits `## Heading {#id}` into its text and its explicit id, if any. */
function parseHeading(line) {
  const heading = HEADING.exec(line)
  if (!heading) return null
  const [, hashes, raw] = heading
  const explicit = EXPLICIT_ID.exec(raw)
  return {
    level: hashes.length,
    text: explicit ? raw.slice(0, explicit.index) : raw,
    id: explicit ? explicit[1] : null,
  }
}

/**
 * @param {string} source raw MDX
 * @returns {string[]} human-readable problems; empty means the page is consistent
 */
export function checkCommandLinks(source) {
  const problems = []
  const lines = source.split("\n")

  // Fenced code can contain anything that looks like a list entry or heading.
  // The closing fence has to use the marker the block opened with, or a ``` in
  // a ~~~ block silently flips the rest of the file inside out.
  const inFence = []
  let openFence = null
  for (const line of lines) {
    const fence = FENCE.exec(line)
    if (fence && (openFence === null || fence[1][0] === openFence[0])) {
      inFence.push(true)
      openFence = openFence === null ? fence[1] : null
      continue
    }
    inFence.push(openFence !== null)
  }

  const content = stripComments(lines, inFence)

  // Sections are collected across the whole file; the list references them from
  // above, and they are the only h3s on the page.
  const sections = new Map()
  content.forEach((line, i) => {
    if (inFence[i]) return
    const heading = parseHeading(line)
    if (!heading || heading.level !== 3 || !heading.text.startsWith("/")) return
    if (sections.has(heading.text)) {
      problems.push(`duplicate section "### ${heading.text}" (line ${i + 1})`)
    }
    // Docusaurus slugs a heading to lowercase and drops the leading slash,
    // unless the author pinned an explicit {#id}.
    sections.set(heading.text, {
      line: i + 1,
      anchor: heading.id ?? heading.text.slice(1).toLowerCase(),
    })
  })

  const start = content.findIndex((line, i) => {
    if (inFence[i]) return false
    const heading = parseHeading(line)
    return heading?.level === 2 && heading.text === LIST_HEADING
  })
  if (start === -1) {
    return [`no "## ${LIST_HEADING}" heading found`]
  }

  const listed = new Map()
  for (let i = start + 1; i < lines.length; i++) {
    if (inFence[i]) continue
    const line = content[i]
    if (parseHeading(line)?.level <= 2) break // next section of the page
    if (line.trim() === "") continue

    const at = `line ${i + 1}`
    const bare = BARE_ENTRY.exec(line)
    const linked = LINKED_ENTRY.exec(line)
    const command = bare?.[1] ?? linked?.[1]

    if (!command) {
      problems.push(
        `${at}: every line in the ${LIST_HEADING} list must be \`- [\`/command\`](#anchor)\`, ` +
          `got: ${line.trim()}`,
      )
      continue
    }
    if (listed.has(command)) {
      problems.push(`${at}: \`${command}\` is listed twice (also line ${listed.get(command)})`)
    }
    listed.set(command, i + 1)

    const section = sections.get(command)
    if (bare) {
      const anchor = section ? section.anchor : command.slice(1)
      problems.push(
        `${at}: \`${command}\` is listed but not linked; ` +
          `use [\`${command}\`](#${anchor})` +
          (section ? "" : ` and add a "### ${command}" section`),
      )
      continue
    }

    if (!section) {
      problems.push(
        `${at}: \`${command}\` links to ${linked[2]} but has no "### ${command}" section`,
      )
    } else if (linked[2] !== `#${section.anchor}`) {
      problems.push(`${at}: \`${command}\` links to ${linked[2]}, expected #${section.anchor}`)
    }
  }

  for (const [command, section] of sections) {
    if (!listed.has(command)) {
      problems.push(`line ${section.line}: "### ${command}" is not in the ${LIST_HEADING} list`)
    }
  }

  return problems
}

// `slack_command_x: str = Field(\n    "/dispatch-x",`. A regex rather than a
// Python parse: the field shape is uniform and enforced by ruff-format, and a
// dependency-free script is what lets the docs CI job skip `npm ci`.
const COMMAND_DEFAULT = /slack_command_\w+\s*:\s*str\s*=\s*Field\(\s*"(\/[^"]+)"/g

/**
 * Every command the Slack plugin registers, read off its configuration schema.
 *
 * @param {string} source raw `config.py`
 * @returns {string[]} the default command strings, in file order
 */
export function registeredCommands(source) {
  return [...source.matchAll(COMMAND_DEFAULT)].map((m) => m[1])
}

/**
 * Commands that exist in the code but appear on none of the documentation
 * pages, which is what `checkCommandLinks` cannot see: it only checks that a
 * page agrees with itself, so a command missing from every page reports ok.
 *
 * The test is a plain substring, not the list/section shape `checkCommandLinks`
 * enforces -- the two pages present commands differently (a linked list and
 * sections on one, a table on the other) and only one of them is worth holding
 * to a fixed shape.
 *
 * @param {string} configSource raw `config.py`
 * @param {string[]} pages raw MDX of every page a command may be documented on
 * @returns {string[]} human-readable problems; empty means every command is documented
 */
export function checkCommandCoverage(configSource, pages) {
  const commands = registeredCommands(configSource)
  if (commands.length === 0) {
    return [`no slack_command_* defaults found in ${CONFIG_SOURCE}; has the field shape changed?`]
  }

  const problems = []
  for (const command of commands) {
    // Word-boundary the trailing edge so `/dispatch-list-task` does not count
    // itself as documented by a mention of `/dispatch-list-tasks`.
    const mentioned = pages.some((page) => new RegExp(`${command}(?![\\w-])`).test(page))
    if (!mentioned) {
      problems.push(`\`${command}\` is registered in ${CONFIG_SOURCE} but documented nowhere`)
    }
  }
  return problems
}

async function checkCoverage(root) {
  let configSource
  try {
    configSource = await readFile(path.join(root, CONFIG_SOURCE), "utf8")
  } catch (error) {
    console.error(`FAIL ${CONFIG_SOURCE}\n  cannot be read: ${error.code ?? error.message}`)
    return false
  }

  const pages = []
  for (const page of DOCUMENTED_IN) {
    try {
      pages.push(await readFile(path.join(root, page), "utf8"))
    } catch (error) {
      console.error(`FAIL ${page}\n  cannot be read: ${error.code ?? error.message}`)
      return false
    }
  }

  const problems = checkCommandCoverage(configSource, pages)
  if (problems.length === 0) {
    console.log(`ok  every command in ${CONFIG_SOURCE} is documented`)
    return true
  }
  console.error("FAIL command coverage")
  for (const problem of problems) console.error(`  ${problem}`)
  return false
}

async function main(targets) {
  let failed = false
  for (const target of targets) {
    let source
    try {
      source = await readFile(target, "utf8")
    } catch (error) {
      console.error(`FAIL ${target}\n  cannot be read: ${error.code ?? error.message}`)
      failed = true
      continue
    }
    const problems = checkCommandLinks(source)
    if (problems.length === 0) {
      console.log(`ok  ${target}`)
      continue
    }
    failed = true
    console.error(`FAIL ${target}`)
    for (const problem of problems) console.error(`  ${problem}`)
  }
  if (failed) process.exitCode = 1
  return !failed
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  const here = path.dirname(fileURLToPath(import.meta.url))
  const linksOk = await main(args.length ? args : [path.join(here, "..", DEFAULT_TARGET)])
  // Only when run over the real pages: an explicit target is a one-off check of
  // some other file and says nothing about whether the docs cover the code.
  const coverageOk = args.length ? true : await checkCoverage(path.join(here, "..", ".."))
  if (!linksOk || !coverageOk) process.exitCode = 1
}
