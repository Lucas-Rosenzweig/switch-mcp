#!/usr/bin/env bash
# Install switch-mcp for the current user:
#   - Python environment (uv sync, with the Bluetooth backend),
#   - `switch-mcp` command in ~/.local/bin,
#   - systemd service started on demand, polkit rule so that `switch-mcp start`
#     and `switch-mcp stop` need no password, root-owned Bluetooth helper,
#   - ~/.config/switch-mcp/config.toml if there is none,
#   - MCP server "switch" in Claude Code, user scope.
# Asks for the sudo password once. Run it again after moving the project or
# pulling a new version; it is idempotent.
#
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove everything above except the configuration,
#                             the stored pairing key and the project itself
set -euo pipefail

project=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
user=$(id -un)
unit=/etc/systemd/system/switch-mcp.service
rules=/etc/polkit-1/rules.d/50-switch-mcp.rules
helper=/usr/local/libexec/switch-mcp/bluetooth-mode
bin_link=$HOME/.local/bin/switch-mcp
config_dir=${XDG_CONFIG_HOME:-$HOME/.config}/switch-mcp

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [[ $EUID -eq 0 ]]; then
    die "run it as your own user, not root: it asks for sudo when needed"
fi

uninstall() {
    say "Stopping and removing the service (sudo)"
    sudo systemctl stop switch-mcp.service 2>/dev/null || true
    sudo rm -f "$unit" "$rules" "$helper"
    sudo rmdir "$(dirname "$helper")" 2>/dev/null || true
    sudo systemctl daemon-reload
    if [[ -L $bin_link ]]; then
        rm -f "$bin_link"
    fi
    if command -v claude >/dev/null; then
        claude mcp remove --scope user switch >/dev/null 2>&1 || true
    fi
    say "Removed. Kept: $config_dir, ~/.local/state/switch-mcp (pairing key, log) and $project"
}

render() {
    # Paths go into a systemd unit and a JavaScript string: keep them plain.
    sed -e "s|@USER@|$user|g" -e "s|@PROJECT@|$project|g" -e "s|@HELPER@|$helper|g" "$1"
}

install() {
    [[ $project =~ ^[A-Za-z0-9/._-]+$ ]] || die "the project path must not contain spaces or special characters: $project"
    [[ $user =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unexpected user name: $user"
    command -v uv >/dev/null || die "uv is required: https://docs.astral.sh/uv/"
    command -v systemctl >/dev/null || die "systemd is required"

    say "Python environment (uv sync --extra bluetooth)"
    (cd "$project" && uv sync --extra bluetooth)

    staging=$(mktemp -d)
    trap 'rm -rf "$staging"' EXIT
    render "$project/deploy/switch-mcp.service.in" >"$staging/switch-mcp.service"
    render "$project/deploy/50-switch-mcp.rules.in" >"$staging/50-switch-mcp.rules"

    say "Service, polkit rule and Bluetooth helper (sudo)"
    sudo install -D -o root -g root -m 0755 "$project/deploy/bluetooth-mode" "$helper"
    sudo install -o root -g root -m 0644 "$staging/switch-mcp.service" "$unit"
    sudo install -D -o root -g root -m 0644 "$staging/50-switch-mcp.rules" "$rules"
    sudo systemctl daemon-reload
    # A running server picks up the new code and unit.
    if systemctl is-active --quiet switch-mcp.service; then
        sudo systemctl restart switch-mcp.service
    fi

    say "Command switch-mcp in ~/.local/bin"
    mkdir -p "$(dirname "$bin_link")"
    ln -sfn "$project/.venv/bin/switch-mcp" "$bin_link"
    case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "    ~/.local/bin is not in PATH; add it (fish: fish_add_path ~/.local/bin)" ;;
    esac

    if [[ ! -f $config_dir/config.toml ]]; then
        say "Configuration $config_dir/config.toml"
        mkdir -p "$config_dir"
        cp "$project/deploy/config.example.toml" "$config_dir/config.toml"
    fi

    local url
    url=$("$project/.venv/bin/switch-mcp" config --url)
    if command -v claude >/dev/null; then
        say "Claude Code: MCP server \"switch\" -> $url"
        claude mcp remove --scope user switch >/dev/null 2>&1 || true
        claude mcp add --scope user --transport http switch "$url"
    else
        echo "    Claude Code not found; register $url in your MCP client"
    fi

    # Leftover from the former deploy/bluetooth-session or deploy/run-with-caps.
    if [[ -f /run/systemd/system/bluetooth.service.d/switch-mcp.conf ]] && ! systemctl is-active --quiet switch-mcp.service; then
        echo "    bluetoothd is still in emulation mode from an earlier session: switch-mcp bt off"
    fi

    say "Done. Start a session with: switch-mcp start   Check the setup with: switch-mcp doctor"
}

case "${1:-}" in
"") install ;;
--uninstall) uninstall ;;
*) die "usage: $0 [--uninstall]" ;;
esac
