"""The site's Model Context Protocol server, and the tool catalogue behind it.

Four modules, in one deliberate order, because the point of the split is that only the
first one knows anything about auctions:

  * ``tools``     -- the catalogue and the dispatcher. Turns :data:`auctions.palette_actions.ACTIONS`
                     into MCP tool descriptors and runs one by name. No HTTP, no JSON-RPC.
  * ``protocol``  -- JSON-RPC 2.0 and the MCP methods. Dicts in, dicts out. No Django.
  * ``transport`` -- the Django view at ``/mcp/``: methods, headers, status codes.
  * ``auth``      -- who is calling. Never a session cookie; see the module for why.

``tools`` is the seam, and it has two callers: the HTTP endpoint that outside agents connect
to, and the command palette's own model, which runs in-process with a live ``request``. Both
get the identical catalogue and the identical dispatcher, so a skill cannot exist for one and
not the other, and a permission cannot be checked differently depending on who asked.

**Replacing the wire format later.** ``protocol`` and ``transport`` are hand-written because the
stateless request/response shape MCP allows is small, and because the auth path is the one thing
worth owning outright. If a library is ever worth adopting -- FastMCP, the MCP Python SDK -- the
swap is those two modules and nothing else: ``tools`` has no HTTP in it, and
``auctions/test_mcp.py`` is written against the endpoint rather than against internals, so it
keeps its meaning across the change.
"""
