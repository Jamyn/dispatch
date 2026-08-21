import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"
import vuetify from "vite-plugin-vuetify"
import monacoEditorPlugin from "vite-plugin-monaco-editor"

import Components from "unplugin-vue-components/vite"

import path from "path"

export default defineConfig({
  plugins: [
    // `include` is redundant for plugin-vue itself -- it is that plugin's own
    // default -- but vite-plugin-vuetify derives its filter from
    // `api.options.include`, which plugin-vue 6 leaves unset unless passed.
    // Without it the vuetify transform runs on every module, not just SFCs.
    vue({ include: /\.vue$/ }),
    vuetify(),
    // Bundles the worker with its own `require("esbuild")`, which resolves to
    // the root esbuild devDependency -- vite 8 declares esbuild a peer and no
    // longer nests one. Removing that devDependency breaks this plugin.
    monacoEditorPlugin({ languageWorkers: ["json"] }),
    Components(),
    {
      resolveId(id) {
        if (id.includes("vee-validate")) {
          return "virtual:vee-validate"
        }
      },
      load(id) {
        if (id.includes("vee-validate")) {
          return `
          let ValidationObserver, ValidationProvider, extend, localize, setInteractionMode, configure, mapFields, ErrorMessage, required, email;
          extend = localize = setInteractionMode = configure = mapFields = required = email = () => {}
          ValidationObserver = ValidationProvider = (_, { slots }) => slots.default({ errors: [], messages: [] })
          export { ValidationObserver, ValidationProvider, extend, localize, setInteractionMode, configure, mapFields, ErrorMessage, required, email };`
        }
      },
    },
  ],
  css: {
    preprocessorOptions: {
      scss: {
        api: "modern",
      },
    },
  },
  server: {
    port: 8080,
    proxy: {
      "^/api": {
        target: "http://127.0.0.1:8000",
        ws: false,
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: [
      {
        find: "@",
        replacement: path.resolve(__dirname, "./src"),
      },
    ],
  },
  build: {
    chunkSizeWarningLimit: 600,
  },
})
