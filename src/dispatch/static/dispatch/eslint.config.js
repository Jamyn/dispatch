const js = require("@eslint/js")
const prettierRecommended = require("eslint-plugin-prettier/recommended")
const vue = require("eslint-plugin-vue")
const vuetify = require("eslint-plugin-vuetify")
const localRules = require("eslint-plugin-local-rules")
const typescriptEslint = require("@typescript-eslint/eslint-plugin")
const tsParser = require("@typescript-eslint/parser")
const globals = require("globals")

module.exports = [
  js.configs.recommended,
  // ESLint 9/10 changed what `recommended` enforces and turned unused-directive
  // reporting on. Held at the ESLint 8 behaviour so this migration reports the
  // same findings it did before; adopting the new rules is #302.
  {
    linterOptions: {
      reportUnusedDisableDirectives: "off",
    },
    rules: {
      "no-unused-vars": ["error", { caughtErrors: "none" }],
      "no-useless-assignment": "off",
    },
  },
  // Before the vue configs, not after: eslint-config-prettier turns off vue's
  // formatting rules, and this order deliberately lets vue re-enable them so
  // the explicit "Conflicts with prettier" block below is what decides.
  prettierRecommended,
  ...vue.configs["flat/strongly-recommended"],
  ...vuetify.configs["flat/base"],
  {
    plugins: {
      "local-rules": localRules,
      "@typescript-eslint": typescriptEslint,
    },
    languageOptions: {
      ecmaVersion: 2020,
      globals: {
        ...globals.browser,
        ...globals.es2021,
        ...globals.node,
      },
      // vue-eslint-parser reads this to parse <script lang="ts"> in SFCs.
      parserOptions: {
        parser: tsParser,
      },
    },
  },
  {
    files: ["test/*"],
    rules: {
      "no-undef": "off",
    },
  },
  {
    rules: {
      "local-rules/icon-button-variant": "error",
      // "local-rules/list-item-children": "error",
      // "local-rules/vee-validate": "error",

      // Conflicts with prettier
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/html-self-closing": [
        "warn",
        {
          html: {
            void: "any",
          },
        },
      ],
      "vue/html-closing-bracket-newline": "off",
      "vue/html-indent": "off",
      "vue/script-indent": "off",

      // Bad defaults
      "vue/valid-v-slot": [
        "error",
        {
          allowModifiers: true,
        },
      ],
      "vue/multi-word-component-names": "off",
      "vue/attribute-hyphenation": "off",
      "vue/require-default-prop": "off",
      "vue/require-explicit-emits": "off",
      "vuetify/no-deprecated-components": "off",
    },
  },
]
