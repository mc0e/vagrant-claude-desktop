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

2. For each file to be created or replaced wholesale, call `write_file(path, content)`.

   For surgical edits to existing files, call `str_replace(path, old_str, new_str)`
   one or more times. The first call copies the original from the project tree;
   subsequent calls on the same path operate on the already-staged copy, so a
   series of edits accumulates correctly. Do not mix `write_file` and `str_replace`
   on the same path within a changeset.

3. Call `end_changeset()` to finalise. This logs a summary of all staged files
   and marks the changeset as ready for review.

4. Tell the user the changeset is ready to review with `project-merge-update`.

### Choosing between write_file and str_replace

Use `str_replace` when editing an existing file and the change is localised —
it is more efficient (only the changed region is transmitted) and makes the
intent of each edit explicit. Use `write_file` when creating a new file or
when the changes are so extensive that a full rewrite is cleaner.

### Example — surgical edits with str_replace

```
begin_changeset("Fix off-by-one error in pagination")
str_replace(
    path="src/pagination.py",
    old_str="    return page * limit",
    new_str="    return (page - 1) * limit",
    description="correct page offset calculation"
)
str_replace(
    path="src/pagination.py",
    old_str="    assert page > 0",
    new_str="    assert page >= 1",
    description="align assertion with corrected logic"
)
end_changeset()
```

### Example — whole-file write

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
2. Populate `current/` from the project tree (first run only)
3. Open Meld for side-by-side review
   - Left pane: `current/` — the current project files
   - Right pane: `proposed/` — the AI-proposed changes
4. Allow selective merging of changes in Meld into the left (`current/`) pane
5. Copy non-empty files from `current/` into the project tree
6. Offer a commit prompt with the changeset description pre-filled

### Partial merges and re-runs

After copying, `current/` reflects exactly what was just written into the
project. Re-running `project-merge-update` resumes the Meld session with that
accurate baseline on the left, so you can adopt further changes from `proposed/`
in subsequent passes. Files left empty in `current/` are skipped on copy —
this is how Meld signals "not adopted".

The next `begin_changeset` call will wipe staging automatically.

## Reading Project Files

Use the `read_file` and `list_directory` tools from `mcp-readonly-fs` to read
any file or directory in the repo. Always read the current state of a file
before proposing changes to it, to avoid working from stale content.

## Scripts Reference

| Script | Purpose |
|--------|---------|
| `project-merge-update` | Review and merge a staged changeset |
