# Claude-Desktop running on a Vagrant Sandbox

## Intent

The idea here is to run claude-desktop with an absolute minimum exposure of the development box.  Claude-Desktop runs in a VirtualBox VM provisioned with Vagrant, and with very limited access to hte host system.  Vagrant appears in a window on the Vagrant Host via X forwarding over ssh.

I give claude-desktop access to my desktop through two mcp connectors (I may combine them at some stage).  My MCP connectors utilise no Anthropic code.


**mcp_readonly_fs.py** gives read only access to the project directory.  It exposes only two operations:
  - list_directory(path)  -- list files and subdirectories under a path
  - read_file(path)       -- return the contents of a file

**mcp_stage_update.py** allows staging of a proposed update to the project code.  Files are staged outside the project directory, in `.claude-staging/<project>/proposed`.

When an update has been staged, the user runs `project-merge-update` from within their project directory.  This copies required files from the project directory into `.claude-staging/<project>/proposed`, runs meld for the user to review the changes, and then optionally copies the modified files into the project directory and optionally commits the changes to git.


## Usage

claude desktop can be run like so:

```
vagrant up
vagrant ssh -- -X claude-desktop
```

It is expected that the tools directory will be added to the user's path, and I use direnv to do this.  

The user wil need to start `mcp_readonly_fs.py` and `mcp_stage_update.py` while within the project directory.  Unless a directory is provided as a positional argument, these walk up the tree from their own location and find the directory containing the .git directory, and serves that directory via MCP.  If no .git file is located, they fall back to serving their current directory.

## Project Status

It's all working, and in use.

Getting the mcp connectors to work was difficult - see my [zonex project](https://github.com/mc0e/zonex) for how I got the IPs and SSL certificates set up.  Also, accessing the mcp servers depends on using `npx mcp-remote [URL]` within the vagrant machine, which cannot be configured via the claude-desktop GUI.  This is provisioned in Vagrantfile, and hard-codes the expected urls of the mcp connectors, including the expected port numbers.

I'm in two minds as to whether to combine the two mcp servers.

