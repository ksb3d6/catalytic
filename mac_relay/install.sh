#!/bin/bash
# Install the Bull Whistle Mac relay under launchd (run from this folder; re-run after editing the script).
# launchd cannot read ~/Desktop (TCC), so the script is COPIED to ~/.local/bin and the plist points there.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
cp "$HERE/aibo_mac_relay.py" "$HOME/.local/bin/aibo-mac-relay.py"
chmod 755 "$HOME/.local/bin/aibo-mac-relay.py"
if [ ! -s "$HOME/.aibo_relay_secret" ]; then
    echo "No ~/.aibo_relay_secret yet. Paste the same string you set as AIBO_MAC_RELAY_SECRET on the aibo-app service:"
    read -r S
    printf '%s' "$S" > "$HOME/.aibo_relay_secret"
    chmod 600 "$HOME/.aibo_relay_secret"
fi
sed "s#__HOME__#$HOME#g" "$HERE/com.bullwhistle.mac-relay.plist" > "$HOME/Library/LaunchAgents/com.bullwhistle.mac-relay.plist"
launchctl unload "$HOME/Library/LaunchAgents/com.bullwhistle.mac-relay.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.bullwhistle.mac-relay.plist"
sleep 2
echo "loaded: $(launchctl list | grep -c com.bullwhistle.mac-relay) · log: ~/aibo-mac-relay.log"
tail -3 "$HOME/aibo-mac-relay.log" 2>/dev/null || true
