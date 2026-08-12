import { defineConfig, mergeConfig } from "vitest/config"
import viteConfig from "./vite.config.js"

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      setupFiles: ["src/tests/setup.js"],
      server: {
        deps: {
          // vuetify ships raw .css imports in its ESM build; it must go
          // through vite's transform pipeline, not node's loader.
          inline: ["vuetify"],
        },
      },
    },
  })
)
