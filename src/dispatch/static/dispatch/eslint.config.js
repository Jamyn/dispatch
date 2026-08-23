const js = require("@eslint/js")
const { defineConfig } = require("eslint/config")
const globals = require("globals")
const prettierRecommended = require("eslint-plugin-prettier/recommended")
const vue = require("eslint-plugin-vue")
const vuetify = require("eslint-plugin-vuetify")
const localRules = require("eslint-plugin-local-rules")
const typescriptEslint = require("@typescript-eslint/eslint-plugin")
const typescriptParser = require("@typescript-eslint/parser")

// Flat config. Later objects win, so order matters:
//   1. eslint:recommended
//   2. prettier (turns formatting rules off, turns prettier/prettier on)
//   3. vue strongly-recommended, then vuetify base (vue re-enables some of its
//      formatting rules *after* prettier disabled them -- the "Conflicts with
//      prettier" block below is what settles those, same as under .eslintrc)
//   4. the project's own rules
module.exports = defineConfig([
  js.configs.recommended,
  {
    // ESLint 9 and 10 changed what `recommended` enforces, and 9 started reporting
    // unused disable directives by default. Held at the ESLint 8 behaviour so
    // the migration reports exactly what it did before; adopting the new
    // defaults is #302.
    linterOptions: {
      reportUnusedDisableDirectives: "off",
    },
    rules: {
      "no-unused-vars": ["error", { caughtErrors: "none" }],
      "no-useless-assignment": "off",
    },
  },
  prettierRecommended,
  ...vue.configs["flat/strongly-recommended"],
  ...vuetify.configs["flat/base"],
  {
    // eslint-plugin-local-rules reads ./eslint-local-rules.js.
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
    },
  },
  {
    // vue-eslint-parser (set on *.vue by eslint-plugin-vue above) hands every
    // <script> block to this parser, so `<script lang="ts">` parses.
    files: ["**/*.vue"],
    languageOptions: {
      parserOptions: {
        parser: typescriptParser,
      },
    },
  },
  {
    // eslint-plugin-vue scopes vue-eslint-parser to *.vue only, so .ts
    // otherwise falls through to espree and fails to parse. eslint-recommended
    // switches off base rules that misread type syntax -- no-undef reads
    // `NodeJS.Timeout` as an undeclared global -- and switches on no-var /
    // prefer-const / prefer-rest-params / prefer-spread.
    files: ["**/*.ts"],
    extends: [typescriptEslint.configs["flat/eslint-recommended"]],
    languageOptions: {
      parser: typescriptParser,
    },
    rules: {
      // eslint-recommended leaves this one on, and it counts a function-type
      // parameter name (`(e: Event) => void`) as an unused variable.
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": "error",
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
])
