# Vagrantfile — Claude Desktop sandbox
#
# Creates a minimal Linux desktop VM with Claude Desktop installed.
# The VM is connected ONLY to a host-only network; it can reach the
# host MCP server on 192.168.56.1:9000 and nothing else locally.
#
# The host MCP server (mcp_readonly_fs.py) must be started separately
# on the host before using Claude Desktop in the VM.
#
#
# Usage:
#   vagrant up
#   vagrant ssh -- -X
#   dbus-launch --exit-with-session claude-desktop

Vagrant.configure("2") do |config|

  config.vm.box = "generic/debian12"
  config.vm.hostname = "claude-sandbox"

  # Host-only network: VM gets 192.168.56.11, host is at 192.168.56.1
  # This is the ONLY network the VM uses beyond the default NAT for SSH.
  # The NAT adapter (adapter 1) is outbound-only for SSH; we leave it
  # in place because Vagrant needs it. Disable shared folders so the
  # host filesystem is not accessible from the VM at all.
  config.vm.network "private_network", ip: "192.168.56.11"

  config.vm.provider "virtualbox" do |vb|
    vb.customize ["modifyvm", :id, "--natdnshostresolver1", "on"]
  end


  # Disable the default /vagrant shared folder — we want no host FS access
  config.vm.synced_folder ".", "/vagrant", disabled: true

  config.vm.provider "virtualbox" do |vb|
    vb.name   = "claude-sandbox"
    vb.memory = 4096   # Claude Desktop needs a reasonable amount
    vb.cpus   = 2

    # Restrict clipboard and drag-and-drop to one-way (host→guest) if desired,
    # or set to "disabled" for full isolation:
    vb.customize ["modifyvm", :id, "--clipboard-mode", "disabled"]
    vb.customize ["modifyvm", :id, "--draganddrop", "disabled"]
    vb.customize ["modifyvm", :id, "--paravirt-provider", "kvm"]
  end

  proxy = `perl -MIO::Socket::INET -e 'exit 1 unless IO::Socket::INET->new(PeerAddr=>"192.168.56.1",PeerPort=>3142,Timeout=>1); print "http://192.168.56.1:3142"' 2>/dev/null`.strip
  ENV['http_proxy'] = ENV['https_proxy'] = proxy unless proxy.empty?

  config.ssh.forward_x11 = true

  config.vm.provision "shell", inline: <<-SHELL
    set -e
    export DEBIAN_FRONTEND=noninteractive

    echo "vagrant:vagrant" | chpasswd

if perl -MIO::Socket::INET -e 'exit 1 unless IO::Socket::INET->new(PeerAddr=>"192.168.56.1",PeerPort=>3142,Timeout=>1)'; then
  export HTTP_PROXY="http://192.168.56.1:3142"
  export http_proxy=${HTTP_PROXY}

   sed -i 's|https://|http://|g' /etc/apt/sources.list # /etc/apt/sources.list.d/*

  echo "Acquire::http::Proxy \\"${HTTP_PROXY}\\";"  >/etc/apt/apt.conf.d/01proxy

  # make DNS work with Mullvad VPN on host.
  sed -i  '/^nameserver / d' /etc/resolv.conf
  echo "nameserver 10.64.0.1" >> /etc/resolv.conf
fi

    # -----------------------------------------------------------------------
    # X11 forwarding support - no X server needed in the VM
    # -----------------------------------------------------------------------
    apt-get update
    apt-get install -y curl wget gnupg2 ca-certificates \
      zstd xauth dbus-x11 libnspr4 libnss3 libatk1.0-0 \
      libatk-bridge2.0-0 libcups2 libcairo2 libgtk-3-0 \
      libpango-1.0-0 libxcomposite1 libxdamage1 libxfixes3 \
      libxrandr2 libgbm1 libxkbcommon0 libasound2 libatspi2.0-0 \
      libx11-xcb1 libgl1-mesa-glx

    # -----------------------------------------------------------------------
    # Install Node
    # Debian's version is too old.
    # -----------------------------------------------------------------------
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -

    apt install -y nodejs

    node_major=$(node --version | sed 's/v\([0-9]*\).*/\1/')
    if [ "$node_major" -lt 20 ]; then
      echo "ERROR: Node.js version $(node --version) is too old, need 20+" >&2
      exit 1
    fi

    echo "Node.js $(node --version) installed successfully"


    # -----------------------------------------------------------------------
    # Add the claude-desktop-debian apt repository
    #   based on https://github.com/aaddrick/claude-desktop-debian
    # -----------------------------------------------------------------------
    curl -fsSL https://pkg.claude-desktop-debian.dev/KEY.gpg \
        | gpg --dearmor \
        | tee /usr/share/keyrings/claude-desktop.gpg > /dev/null

    echo "deb [signed-by=/usr/share/keyrings/claude-desktop.gpg arch=amd64] \
      https://pkg.claude-desktop-debian.dev stable main" \
      | tee /etc/apt/sources.list.d/claude-desktop.list

    apt-get update
    apt-get install -y  claude-desktop

    # Fix X11 forwarding — sshd on Debian 12 fails to bind X11 socket
    # with default X11UseLocalhost yes in a VirtualBox VM
    sed -i 's/#X11UseLocalhost yes/X11UseLocalhost no/' /etc/ssh/sshd_config
    grep -q '^X11UseLocalhost' /etc/ssh/sshd_config \
      || echo "X11UseLocalhost no" >> /etc/ssh/sshd_config
    systemctl restart sshd


    # Electron sandbox workaround for VirtualBox VM environment.
    # The sed pattern must match the actual launcher. We append flags just
    # before the final exec line to be robust against launcher refactors.
    # Also set chrome-sandbox to setuid-root so --no-sandbox is not the
    # only defence (belt-and-suspenders: setuid path works if present,
    # --no-sandbox is the fallback when it cannot).
    SANDBOX=/usr/lib/claude-desktop/node_modules/electron/dist/chrome-sandbox
    if [ -f "$SANDBOX" ]; then
      chown root:root "$SANDBOX"
      chmod 4755 "$SANDBOX"
    fi

    # Patch the launcher: append --no-sandbox --disable-seccomp-filter-sandbox
    # to the electron exec line. The pattern targets the exec call regardless
    # of whether electron_args is spelled with or without quotes.
    sed -i \
      's|electron_args+=("$app_path")|electron_args+=("$app_path" "--no-sandbox" "--disable-seccomp-filter-sandbox")|' \
      /usr/bin/claude-desktop
    # Verify the patch landed; abort provisioning loudly if not.
    grep -q -- '--no-sandbox' /usr/bin/claude-desktop \
      || { echo "ERROR: --no-sandbox patch did not match launcher. Inspect /usr/bin/claude-desktop."; exit 1; }


    # -----------------------------------------------------------------------
    # Claude Desktop MCP configuration
    # Points to the host-only MCP server running on the host machine.
    # Written for the 'vagrant' user; adjust if you add other users.
    # -----------------------------------------------------------------------
    CLAUDE_CFG_DIR="/home/vagrant/.config/Claude"
    mkdir -p "$CLAUDE_CFG_DIR"
    chown -R vagrant:vagrant "$CLAUDE_CFG_DIR"


# Update only the mcpServers section, preserving any other config
# npx connections cannot be configured inside the GUI
    python3 - "$CLAUDE_CFG_DIR/claude_desktop_config.json" <<'EOF'
import json, sys
path = sys.argv[1]
try:
    cfg = json.load(open(path))
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
cfg["mcpServers"] = {
    "project-filesystem": {
        "command": "npx",
        "args": ["mcp-remote", "https://vhost.x.mc0e.net:9000/mcp"]
    },
    "merge-update": {
        "command": "npx",
        "args": ["mcp-remote", "https://vhost.x.mc0e.net:9001/mcp"]
    }
}
json.dump(cfg, open(path, "w"), indent=2)
print(f"Updated mcpServers in {path}")
EOF
    chown vagrant:vagrant "$CLAUDE_CFG_DIR/claude_desktop_config.json"

    echo "Provisioning complete.  Run vagrant ssh -- -X claude-desktop-sandboxed"
  SHELL

end
