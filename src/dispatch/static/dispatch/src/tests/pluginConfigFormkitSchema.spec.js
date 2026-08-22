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
