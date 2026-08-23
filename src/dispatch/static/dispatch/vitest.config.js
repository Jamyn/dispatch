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
      coverage: {
        provider: "v8",
        // `include` is what makes never-imported components count. Without it
        // v8 reports only files a test loaded, which for ten spec files would
        // read as high coverage of a tiny fraction of the app.
        // scripts/ is here because lockfile-sync runs it as a gate: left out,
        // its lines land in the diff with no report and codecov reads the
        // whole file as untested.
        include: ["src/**", "scripts/**"],
        // The specs themselves, like the Python suite's own files, are not the
        // thing being measured.
        exclude: ["src/tests/**"],
        reporter: ["text-summary", "lcov"],
      },
    },
  }),
)
