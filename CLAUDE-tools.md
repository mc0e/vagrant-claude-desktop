# Claude Tools

This file documents the MCP tools and scripts available for AI-assisted
development in this project. Reference it at the start of sessions where
you will be proposing file changes.

## PATH export

The user should have added the tools directory to their PATH.  This will typically 
be done using direnv and a .envrc file in the project directory.

## MCP Servers

Two MCP servers run on the host machine alongside Claude Desktop:

| Server | Script | Default | Purpose |
|--------|--------|---------|---------|
| `mcp-readonly-fs` | `mcp_readonly_fs.py` | `127.0.0.1:9001` | Read project files |
| `mcp-stage-update` | `mcp_stage_update.py` | `127.0.0.1:9001` | Stage proposed changes |

Both servers are configured in Claude Desktop and should be available as MCP
tools at the start of each session.

## Proposing Changes

When proposing changes to project files, always use the staging workflow rather
than producing diffs, zip files, or inline file content.

### Workflow

1. Call `begin_changeset(description)` with a brief description of the change.
   This wipes any previously staged files and starts a new changeset. The
   description will be suggested as the git commit message.

2. Call `write_file(path, content)` for each file to be created or modified.
   Paths are relative to the repo root (e.g. `scripts/foo`, `Design/README.md`).
   A file cannot be written twice in the same changeset.

3. Call `end_changeset()` to finalise. This logs a summary of all staged files
   and marks the changeset as ready for review.

4. Tell the user the changeset is ready to review with `project-merge-update`.

### Example

```
begin_changeset("Add input validation to registration flow")
write_file("src/validation.py", "...")
write_file("tests/test_validation.py", "...")
end_changeset()
```

### Staged file location

Files are staged at `~/.claude-staging/<repo-name>/` on the host. Nothing can
be written outside this directory.

## Reviewing and Merging Changes

Run `project-merge-update` from anywhere inside the repo:

```bash
project-merge-update
```

This will:
1. Read the staged changeset manifest and display a summary
2. Open Meld for side-by-side review against the current working tree
3. Allow selective merging of changes in Meld
4. Offer a commit prompt with the changeset description pre-filled
5. Leave staged files in place after commit (safe to re-run if needed)

The next `begin_changeset` call will wipe staging automatically.

## Reading Project Files

Use the `read_file` and `list_directory` tools from `mcp-readonly-fs` to read
any file or directory in the repo. Always read the current state of a file
before proposing changes to it, to avoid working from stale content.

## Scripts Reference

| Script | Purpose |
|--------|---------|
| `project-merge-update` | Review and merge a staged changeset |
