# Vendored browser bundles

## `ext_apps.js`

`@modelcontextprotocol/ext-apps` **1.7.5**, the `app-with-deps` export:

```bash
curl -sSLo auctions/mcp/vendor/ext_apps.js \
  https://unpkg.com/@modelcontextprotocol/ext-apps@1.7.5/dist/src/app-with-deps.js
```

This is the `App` class an MCP-app widget uses to talk to its host — the postMessage handshake,
`ontoolresult`, `callServerTool`, `openLink`, host theming. It is vendored rather than fetched
because the widget iframe's CSP blocks every external script, and vendored **unmodified** so the
command above is a diff: `auctions.mcp.widgets._bundle()` rewrites the trailing `export{…}` into a
`globalThis.ExtApps` assignment at render time, since an inline `<script type="module">` cannot
export.

Do not edit it. To upgrade, re-run the curl with a new version and run
`auctions.test_mcp_widgets`, which fails if the rewrite no longer finds an export statement.
