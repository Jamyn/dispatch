import { mount } from "@vue/test-utils"
import { expect, describe, it, beforeEach, afterEach } from "vitest"
import RichEditor from "@/components/RichEditor.vue"

// tiptap v3 changed `setContent(content, emitUpdate)` to
// `setContent(content, options)` with `emitUpdate` defaulting to true. The old
// positional `false` still type-checks in a plain <script setup> block and
// still builds, so the only thing that catches the regression is asserting
// that a prop-driven set stays silent.

const flush = async (wrapper) => {
  await wrapper.vm.$nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
}

let wrapper

const build = (props = {}) => mount(RichEditor, { props, attachTo: document.body })

beforeEach(() => {
  wrapper = null
})

afterEach(() => {
  wrapper?.unmount()
})

describe("RichEditor", () => {
  it("renders the initial modelValue into the editor", async () => {
    wrapper = build({ modelValue: "<p>hello</p>" })
    await flush(wrapper)

    expect(wrapper.text()).toContain("hello")
  })

  it("falls back to the content prop when modelValue is empty", async () => {
    wrapper = build({ content: "<p>legacy</p>" })
    await flush(wrapper)

    expect(wrapper.text()).toContain("legacy")
  })

  // The two assertions below are a pair: the first proves the write happened,
  // the second proves it stayed silent. Without the first, the second passes
  // just as well when setContent never ran at all.
  it("applies a modelValue prop change to the document", async () => {
    wrapper = build({ modelValue: "<p>first</p>" })
    await flush(wrapper)

    await wrapper.setProps({ modelValue: "<p>second</p>" })
    await flush(wrapper)

    expect(wrapper.text()).toContain("second")
    expect(wrapper.text()).not.toContain("first")
  })

  it("does not emit update:modelValue when the modelValue prop drives the change", async () => {
    wrapper = build({ modelValue: "<p>first</p>" })
    await flush(wrapper)

    await wrapper.setProps({ modelValue: "<p>second</p>" })
    await flush(wrapper)

    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
  })

  it("does not emit update:modelValue when the content prop drives the change", async () => {
    wrapper = build({ content: "<p>first</p>" })
    await flush(wrapper)

    await wrapper.setProps({ content: "<p>second</p>" })
    await flush(wrapper)

    expect(wrapper.text()).toContain("second")
    expect(wrapper.emitted("update:modelValue")).toBeUndefined()
  })

  it("re-emits content the user typed", async () => {
    wrapper = build({ modelValue: "<p>first</p>" })
    await flush(wrapper)

    // A user edit goes through the editor itself, not the prop, so it must
    // still emit -- otherwise suppressing the prop path would break v-model.
    wrapper.vm.$.setupState.editor.commands.setContent("<p>typed</p>")
    await flush(wrapper)

    const emitted = wrapper.emitted("update:modelValue")
    expect(emitted).toBeTruthy()
    expect(emitted.at(-1)[0]).toContain("typed")
  })

  it("exposes the placeholder wired through @tiptap/extensions", async () => {
    wrapper = build({ placeholder: "Describe the incident" })
    await flush(wrapper)

    const paragraph = wrapper.element.querySelector("p")
    expect(paragraph?.getAttribute("data-placeholder")).toBe("Describe the incident")
  })

  it("honours the disabled prop", async () => {
    wrapper = build({ modelValue: "<p>x</p>", disabled: true })
    await flush(wrapper)

    expect(wrapper.vm.$.setupState.editor.isEditable).toBe(false)
  })
})

describe("RichEditor authoring surface", () => {
  // StarterKit v3 turns Link and Underline on by default. Keeping the v2
  // authoring surface is deliberate, so assert it rather than trusting the
  // configure() call to stay put.
  it("has no link or underline mark in its schema", async () => {
    // Autolink fires on typed input, which happy-dom cannot drive faithfully.
    // The schema is the thing that decides what is representable at all, so
    // assert there instead of simulating keystrokes.
    const wrapper = build({ modelValue: "<p>x</p>" })
    await flush(wrapper)

    const marks = Object.keys(wrapper.vm.$.setupState.editor.schema.marks)
    expect(marks).not.toContain("link")
    expect(marks).not.toContain("underline")
    wrapper.unmount()
  })

  it("drops link and underline marks from incoming content", async () => {
    const wrapper = build({
      modelValue: '<p><a href="https://example.com">x</a><u>y</u></p>',
    })
    await flush(wrapper)

    const html = wrapper.vm.$.setupState.editor.getHTML()
    expect(html).not.toContain("<a ")
    expect(html).not.toContain("<u>")
    wrapper.unmount()
  })

  it("still supports the marks the v2 editor had", async () => {
    const wrapper = build({
      modelValue: "<p><strong>b</strong><em>i</em><s>s</s><code>c</code></p>",
    })
    await flush(wrapper)

    const html = wrapper.vm.$.setupState.editor.getHTML()
    for (const tag of ["<strong>", "<em>", "<s>", "<code>"]) {
      expect(html).toContain(tag)
    }
    wrapper.unmount()
  })
})
