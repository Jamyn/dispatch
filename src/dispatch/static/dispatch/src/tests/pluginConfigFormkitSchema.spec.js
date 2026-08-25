/**
 * `convertToFormkit` branched on `value.type` alone, so every pydantic
 * `SecretStr` -- which emits `{"type": "string", "format": "password"}` -- came
 * out as a cleartext text input, and `{"type": "integer"}` matched no branch at
 * all and was dropped from the form entirely (#109).
 *
 * Every schema fragment below is copied verbatim from what
 * `plugin.configuration_schema.schema()` actually emits, not hand-written from
 * the pydantic source: the two disagree about where `format` ends up on an
 * `Optional[SecretStr]`, which is exactly the case the fix has to handle.
 */

import { beforeEach, describe, expect, it, test, vi } from "vitest"
import { mount } from "@vue/test-utils"
import { plugin as formkitPlugin, defaultConfig, FormKit, FormKitSchema } from "@formkit/vue"
import { defineComponent, ref } from "vue"

vi.mock("@/plugin/api", () => ({ default: { getAll: vi.fn(), getAllInstances: vi.fn() } }))
vi.mock("@/api", () => ({ default: { get: vi.fn() } }))

import pluginStore from "@/plugin/store"

/** `convertToFormkit` is module-private; SET_SELECTED is how the app reaches it. */
const convert = (properties, extra = {}) => {
  const state = { selected: {} }
  pluginStore.mutations.SET_SELECTED(state, {
    configuration_schema: { description: "Test plugin", properties, ...extra },
  })
  // Drop the leading `<h1>` description element.
  return state.selected.formkit_configuration_schema.slice(1)
}
const convertOne = (property) => convert({ field: property })[0]

// --- verbatim fragments of the emitted schema -------------------------------

const ZOOM_CLIENT_SECRET = {
  description:
    "Client secret of the Server-to-Server OAuth app. Treat this as a credential: anyone holding it can act on the account through the app's scopes.",
  format: "password",
  title: "Client Secret",
  type: "string",
  writeOnly: true,
}

/** `Optional[SecretStr]`: no top-level type, `format` buried in `anyOf`. */
const SLACK_SOCKET_MODE_APP_TOKEN = {
  anyOf: [{ format: "password", type: "string", writeOnly: true }, { type: "null" }],
  description: "Token used when plugin is in socket mode.",
  title: "Socket Mode App Token",
}

const ZOOM_CLIENT_ID = {
  description: "Client ID of the Server-to-Server OAuth app.",
  title: "Client ID",
  type: "string",
}

const CONFLUENCE_API_URL = {
  description: "This URL is used for communication with API.",
  format: "uri",
  minLength: 1,
  title: "API URL",
  type: "string",
}

const PAGERDUTY_FROM_EMAIL = {
  description: "This the email to put into the 'From' field of any page requests.",
  format: "email",
  title: "From Email",
  type: "string",
}

const ZOOM_DEFAULT_DURATION_MINUTES = {
  default: 1440,
  description:
    "Default duration in minutes for conference meetings. Defaults to 1440 minutes (1 day), which is also Zoom's maximum.",
  maximum: 1440,
  minimum: 1,
  title: "Default Meeting Duration (Minutes)",
  type: "integer",
}

const AWS_BATCH_SIZE = {
  default: 10,
  description: "Number of messages to retrieve from SQS.",
  maximum: 10,
  title: "Batch Size",
  type: "integer",
}

const GOOGLE_DEFAULT_DURATION_MINUTES = {
  default: 1440,
  description:
    "Default duration in minutes for conference events. Defaults to 1440 minutes (1 day).",
  title: "Default Event Duration (Minutes)",
  type: "integer",
}

/**
 * A referenced enum. Pydantic v2 emits a bare `$ref` on the property and puts
 * the members under `$defs`, not `allOf`/`definitions` (#293).
 */
const CONFLUENCE_HOSTING_TYPE = {
  $ref: "#/$defs/HostingType",
  default: "cloud",
  description: "Defines the type of deployment.",
  title: "Hosting Type",
}

const CONFLUENCE_DEFS = {
  HostingType: {
    description: "Type of Atlassian Confluence deployment.",
    enum: ["cloud", "server"],
    title: "HostingType",
    type: "string",
  },
}

const CONFLUENCE_OPEN_ON_CLOSE = {
  default: false,
  description:
    "Controls the visibility of resources on incident close. If enabled Dispatch will make all resources visible to the entire workspace.",
  title: "Open On Close",
  type: "boolean",
}

describe("secrets never convert to a cleartext input", () => {
  test.each([
    ["a SecretStr", ZOOM_CLIENT_SECRET],
    ["an Optional[SecretStr] carrying format inside anyOf", SLACK_SOCKET_MODE_APP_TOKEN],
  ])("%s becomes a password input", (_label, property) => {
    expect(convertOne(property).$formkit).toBe("password")
  })

  test.each([
    ["a plain string", ZOOM_CLIENT_ID],
    ["format: uri", CONFLUENCE_API_URL],
    ["format: email", PAGERDUTY_FROM_EMAIL],
  ])("%s stays a text input", (_label, property) => {
    expect(convertOne(property).$formkit).toBe("text")
  })

  it("carries no value or default for a secret, so nothing is echoed into the schema", () => {
    const obj = convertOne(ZOOM_CLIENT_SECRET)
    expect(obj).not.toHaveProperty("value")
    expect(obj).not.toHaveProperty("default")
  })

  it("opts secrets out of browser credential autofill", () => {
    expect(convertOne(ZOOM_CLIENT_SECRET).autocomplete).toBe("new-password")
    expect(convertOne(SLACK_SOCKET_MODE_APP_TOKEN).autocomplete).toBe("new-password")
    // A non-secret string must not pick the attribute up.
    expect(convertOne(ZOOM_CLIENT_ID)).not.toHaveProperty("autocomplete")
  })
})

describe("integer fields are rendered", () => {
  it("becomes a number input that yields an integer, seeded with the schema default", () => {
    expect(convertOne(ZOOM_DEFAULT_DURATION_MINUTES)).toMatchObject({
      $formkit: "number",
      name: "field",
      label: "Default Meeting Duration (Minutes)",
      number: "integer",
      value: 1440,
      validation: "min:1|max:1440",
    })
  })

  it("carries only the bounds the schema actually declares", () => {
    expect(convertOne(AWS_BATCH_SIZE).validation).toBe("max:10")
    expect(convertOne(GOOGLE_DEFAULT_DURATION_MINUTES)).not.toHaveProperty("validation")
  })
})

describe("a referenced enum is rendered as a select", () => {
  it("resolves the $ref through $defs into options, seeded with the schema default", () => {
    const obj = convert({ hosting_type: CONFLUENCE_HOSTING_TYPE }, { $defs: CONFLUENCE_DEFS })[0]
    expect(obj).toMatchObject({
      $formkit: "select",
      name: "hosting_type",
      label: "Hosting Type",
      help: "Defines the type of deployment.",
      options: [
        { label: "cloud", value: "cloud" },
        { label: "server", value: "server" },
      ],
      value: "cloud",
    })
  })

  it("survives a $ref that resolves to nothing without dropping later fields", () => {
    const schema = convert({
      hosting_type: CONFLUENCE_HOSTING_TYPE,
      open_on_close: CONFLUENCE_OPEN_ON_CLOSE,
    })
    expect(schema).toHaveLength(2)
    expect(schema[0]).toEqual({})
    expect(schema[1]).toMatchObject({ $cmp: "FormKit", props: { name: "open_on_close" } })
  })
})

it("leaves boolean fields alone", () => {
  expect(convertOne(CONFLUENCE_OPEN_ON_CLOSE)).toMatchObject({
    $cmp: "FormKit",
    props: { name: "field", type: "checkbox" },
  })
})

// --- the generated schema, actually rendered --------------------------------

const Harness = defineComponent({
  components: { FormKit, FormKitSchema },
  props: {
    schema: { type: Array, required: true },
    initial: { type: Object, default: () => ({}) },
  },
  setup(props) {
    return { configuration: ref({ ...props.initial }) }
  },
  template: `<FormKit type="form" v-model="configuration" :actions="false">
    <FormKitSchema :schema="schema" />
  </FormKit>`,
})

describe("the generated schema, mounted through FormKitSchema", () => {
  let wrapper

  beforeEach(() => {
    wrapper = mount(Harness, {
      props: {
        schema: convert({
          client_secret: ZOOM_CLIENT_SECRET,
          default_duration_minutes: ZOOM_DEFAULT_DURATION_MINUTES,
        }),
        // What the API hands back for a saved instance: SecretStr serialises masked.
        initial: { client_secret: "**********" },
      },
      global: { plugins: [[formkitPlugin, defaultConfig]] },
    })
  })

  it("masks the secret in the DOM and keeps the browser out of it", () => {
    const input = wrapper.find('input[name="client_secret"]')
    expect(input.attributes("type")).toBe("password")
    expect(input.attributes("autocomplete")).toBe("new-password")
    // The API never sends the real secret, and the fix must not add a path that does.
    expect(wrapper.html()).not.toContain('type="text"')
  })

  it("round-trips an integer edit back into the form model as a number", async () => {
    const input = wrapper.find('input[name="default_duration_minutes"]')
    expect(input.exists()).toBe(true)
    expect(input.element.value).toBe("1440")

    await input.setValue("45")
    await vi.waitUntil(() => wrapper.vm.configuration.default_duration_minutes === 45)
    expect(typeof wrapper.vm.configuration.default_duration_minutes).toBe("number")
  })

  it("rejects a value outside the schema's bounds", async () => {
    const input = wrapper.find('input[name="default_duration_minutes"]')
    await input.setValue("5000")
    await input.trigger("blur")
    await vi.waitUntil(() => wrapper.find(".formkit-message").exists(), { timeout: 5000 })
    expect(wrapper.find(".formkit-message").text()).toContain("1440")
  })
})

describe("the confluence schema, mounted through FormKitSchema", () => {
  const mountConfluence = () =>
    mount(Harness, {
      props: {
        schema: convert(
          { hosting_type: CONFLUENCE_HOSTING_TYPE, api_url: CONFLUENCE_API_URL },
          { $defs: CONFLUENCE_DEFS },
        ),
      },
      global: { plugins: [[formkitPlugin, defaultConfig]] },
    })

  it("renders a select offering every enum member, defaulted to cloud", () => {
    const select = mountConfluence().find('select[name="hosting_type"]')
    expect(select.exists()).toBe(true)
    expect(select.findAll("option").map((o) => o.element.value)).toEqual(["cloud", "server"])
    expect(select.element.value).toBe("cloud")
  })

  // Confluence's default is also the first option, so that test alone cannot
  // tell a seeded select from an unseeded one. This one can.
  it("seeds the select from a default that is not the first member", () => {
    const wrapper = mount(Harness, {
      props: {
        schema: convert(
          { hosting_type: { ...CONFLUENCE_HOSTING_TYPE, default: "server" } },
          { $defs: CONFLUENCE_DEFS },
        ),
      },
      global: { plugins: [[formkitPlugin, defaultConfig]] },
    })
    expect(wrapper.find('select[name="hosting_type"]').element.value).toBe("server")
  })

  // A saved instance's stored value must win over the schema default, or
  // editing any other field would silently reset hosting_type to cloud.
  it("lets a saved instance's value override the schema default", () => {
    const wrapper = mount(Harness, {
      props: {
        schema: convert({ hosting_type: CONFLUENCE_HOSTING_TYPE }, { $defs: CONFLUENCE_DEFS }),
        initial: { hosting_type: "server" },
      },
      global: { plugins: [[formkitPlugin, defaultConfig]] },
    })
    expect(wrapper.find('select[name="hosting_type"]').element.value).toBe("server")
    expect(wrapper.vm.configuration.hosting_type).toBe("server")
  })

  it("puts the operator's choice into the form model", async () => {
    const wrapper = mountConfluence()
    await wrapper.find('select[name="hosting_type"]').setValue("server")
    await vi.waitUntil(() => wrapper.vm.configuration.hosting_type === "server")
    expect(wrapper.vm.configuration.hosting_type).toBe("server")
  })
})
