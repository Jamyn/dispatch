// @ts-check
// Note: type annotations allow type checking and IDEs autocompletion

const { themes } = require("prism-react-renderer")
const lightCodeTheme = themes.github
const darkCodeTheme = themes.dracula

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: "Dispatch - Documentation",
  tagline: "Incident Management for Everyone",
  favicon: "img/favicon.ico",

  // Set the production url of your site here
  url: "https://jamyn.github.io/",
  // Set the /<baseUrl>/ pathname under which your site is served
  // For GitHub pages deployment, it is often '/<projectName>/'
  baseUrl: "/dispatch",

  // GitHub pages deployment config.
  // If you aren't using GitHub pages, you don't need these.
  organizationName: "jamyn", // Usually your GitHub org/user name.
  projectName: "dispatch", // Usually your repo name.
  trailingSlash: false,

  onBrokenLinks: "throw",

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: "warn",
    },
  },

  // Even if you don't use internalization, you can use this field to set useful
  // metadata like html lang. For example, if your site is Chinese, you may want
  // to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },
  plugins: [
    [
      require.resolve("@cmfcmf/docusaurus-search-local"),
      {
        indexPages: true,
        // Must track `blog: false` in the classic preset below — the plugin
        // hard-errors at postBuild if indexBlog is on with no blog plugin.
        indexBlog: false,
        style: undefined,
      },
    ],
  ],

  presets: [
    [
      "classic",
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: require.resolve("./sidebars.js"),
          editUrl: ({ docPath }) =>
            `https://github.com/Jamyn/dispatch/edit/main/docs/docs/${docPath}`,
        },
        // This site has no blog/ directory. Docusaurus 2 emitted nothing for the
        // preset's default-on blog; v3 emits an empty /blog index page instead.
        blog: false,
        theme: {
          customCss: require.resolve("./src/css/custom.css"),
        },
      }),
    ],
    [
      "redocusaurus",
      {
        // Plugin Options for loading OpenAPI files
        specs: [
          {
            spec: "scripts/openapi.yaml",
            route: "/docs/api/",
          },
        ],
        // Theme Options for modifying how redoc renders them
        theme: {
          // Change with your site colors
          primaryColor: "#E50914",
        },
      },
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      // Replace with your project's social card
      image: "img/docusaurus-social-card.jpg",
      navbar: {
        title: "Dispatch",
        items: [
          { to: "/docs/user-guide", label: "User Guide", position: "left" },
          { to: "/docs/administration", label: "Administration", position: "left" },
          { to: "/docs/api", label: "API", position: "left" },
          {
            to: "/docs/support",
            label: "Support",
            position: "left",
          },

          {
            href: "https://github.com/Jamyn/dispatch",
            position: "right",
            className: "header-github-link",
            "aria-label": "GitHub repository",
          },
        ],
      },
      footer: {
        style: "dark",
        links: [
          {
            title: "More",
            items: [
              { label: "Changelog", to: "/docs/changelog" },
              { label: "License", to: "/docs/license" },
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} Dispatch Documentation Built with Docusaurus.`,
      },
      prism: {
        theme: lightCodeTheme,
        darkTheme: darkCodeTheme,
      },
    }),
}

module.exports = config
