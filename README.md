# Claude-Desktop running on a Vagrant Sandbox

## Intent

The idea here is to run claude-desktop with an absolute minimum exposure of the development box.  Claude-Desktop runs in a VirtualBox VM provisioned with Vagrant, and with access to the host system through a highly constrained custom MCP connector which utilises no Anthropic code.

Vagrant appears in a window on the Vagrant Host via X forwarding over ssh.

My MCP connector exposes only two operations - list files, and read file content, though other connectors are possible.

## Usage

It is expected that `mcp-serve` and `mcp_readonly_fs.py` or something similar will be copied into the project directory where work is being done.  `mcp_readonly_fs.py` walks up the tree from its own location and finds the directory containing the .git directory, and serves that directory via MCP on port 9000.

claude desktop can be run like so:

```
vagrant up
vagrant ssh -- -X claude-desktop
```

## Project Status

After a surprisingly hard fight, the vagrant machine is running, and displaying via X.  A little more work is required to get the MCP connector working.

This started out as a sub-project of what I actually wanted to be working on, but has reached a point where it clearly deserves its own repository.  I'll make changes to the interface to reflect that - currently it still expects that it's in a sub-directory of the project being worked on.
