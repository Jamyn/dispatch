module.exports = {
  root: true,
  plugins: ["eslint-plugin-local-rules", "@typescript-eslint"],
  extends: [
    "eslint:recommended",
    "plugin:prettier/recommended",
    "plugin:vue/strongly-recommended",
    "plugin:vuetify/base",
  ],
  parserOptions: {
    ecmaVersion: 2020,
    parser: "@typescript-eslint/parser",
  },
  env: {
    browser: true,
    es2021: true,
    node: true,
  },
  overrides: [
    {
      // eslint-plugin-vue scopes vue-eslint-parser to *.vue only, so .ts
      // otherwise falls through to espree and fails to parse. eslint-recommended
      // switches off base rules that misread type syntax -- no-undef reads
      // `NodeJS.Timeout` as an undeclared global -- and switches on no-var /
      // prefer-const / prefer-rest-params / prefer-spread.
      files: ["*.ts"],
      extends: ["plugin:@typescript-eslint/eslint-recommended"],
      parser: "@typescript-eslint/parser",
      rules: {
        // eslint-recommended leaves this one on, and it counts a function-type
        // parameter name (`(e: Event) => void`) as an unused variable.
        "no-unused-vars": "off",
        "@typescript-eslint/no-unused-vars": "error",
      },
    },
    {
      files: ["test/*"],
      rules: {
        "no-undef": "off",
      },
    },
  ],
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
}
