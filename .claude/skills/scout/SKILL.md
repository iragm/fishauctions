---
name: scout
description: Read the live site through the admin connector (errors, usability, palette transcripts, species gaps, feature requests) and turn what it shows into feature suggestions and data-change proposals for the owner to approve. Use when running as the scout routine, or when asked to scout, check on the site, or find what needs doing.
---

# Scout

You read production through the **admin connector** (`/mcp/admin/`, `auctions/mcp/admin.py`). It
cannot change the site. You leave two kinds of output, and nothing else:

- **`suggest_feature`**: something the code should do differently. It joins the feature requests as
  `new`. The owner marks the ones to build `planned`, and the build-requests routine builds those.
- **`propose_change`**: a change to the site's *data*, such as setting a lot's species. It waits on
  `/admin-dashboard/proposals/` until the owner presses Approve.

**Never** open GitHub issues, and never write a member's name, username, email or words into the
repository, an issue or a PR. The repository is public. Evidence in a suggestion is counts, page
names and dates.

## The run

1. `site_health`. If it shows a new error kind or a migration not applied, `read_logs` with
   `level=ERROR` and `contains=` that error. Then find the cause in this repository. A real bug is a
   `suggest_feature` naming the file and line. Leave 500s alone: the hourly health check fixes those
   straight away, with a PR into `master`.
2. GitHub issues: intake and closing, exactly as the `github-issues` skill says. Labelling,
   commenting on and closing an existing issue are the only GitHub writes a scout makes.
3. `list_feature_requests status=all` first, so you don't suggest anything already there.
4. Read, in this order, and stop when you have enough:
   - `read_admin_page usability query=days=7`
   - `command_palette_analytics`: read the exchanges themselves. A second query soon after a first
     is chaining, not failure.
   - `assistant_skill_requests` (members' own asks)
   - `species_gaps`
   - `admin_session_replay` for the sessions behind a funnel drop
5. File at most **five** suggestions, each with its evidence. One well-evidenced suggestion beats
   five guesses.
6. Fix data in batches with `propose_change`, one proposal per kind of fix: for example, the species
   for every lot on the gaps page whose match you are sure of (`set_lot_species`).
7. For each `planned` request: if its commit (`feature request #N` in the subject) is on `master`
   and `site_health`'s commit includes it, propose `set_request_status` → `done`.
8. End with a few lines: issues queued and closed, what you filed, what you proposed, anything you
   couldn't tell.

## What the owner has already decided

- About five people use the palette, so a rate is one person's afternoon. Quote exchanges, not
  percentages.
- One sentence at most on any page. Don't suggest explanations of machinery that just works.
- PageView rows are kept forever. Never suggest purging them; name the slow query instead.
- Don't suggest promoting the palette or MCP to users yet.
