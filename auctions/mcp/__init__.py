"""The site's Model Context Protocol server, and the tool catalogue behind it.

``tools`` turns :data:`auctions.palette_actions.ACTIONS` into MCP tool descriptors and dispatches
by name, with no HTTP in it; ``protocol`` is JSON-RPC 2.0; ``transport`` is the Django view at
``/mcp/``; ``auth`` decides who is calling. ``resources``, ``prompts``, ``widgets``, ``icons`` and
``cimd`` are the rest.

``tools`` is the one seam both the HTTP endpoint and the in-process command palette call, so a
skill and its permission check cannot differ by caller.
"""
