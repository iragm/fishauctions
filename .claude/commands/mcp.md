---
description: Run the MCP and command-palette test group, and smoke-test the endpoint
allowed-tools: Bash(docker exec django python3 manage.py test *), Bash(curl *), Read
---

```
docker exec -it django python3 manage.py test \
  auctions.test_mcp auctions.test_mcp_widgets auctions.test_mcp_resources \
  auctions.test_mcp_permissions auctions.test_source_code auctions.test_palette_account \
  auctions.test_palette_skills auctions.test_palette_assist auctions.test_palette_routes
```

Endpoint smoke test -- an unauthenticated call must be a 401 carrying the resource-metadata pointer:

```
curl -si -X POST http://127.0.0.1/mcp/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}' \
  | head -20
# expect 401 + WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource"
```

Adding a URL costs two entries: a new named URL or POST view has to be catalogued in
`palette_actions` or excused in `NOT_A_SKILL`, or `test_palette_skills` fails the build. An excuse
has to be about the *capability*, not about the palette.
