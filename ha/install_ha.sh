#!/bin/sh
# Installs the phonejack package into a Home Assistant *container* install.
# Usage:  sudo sh ha/install_ha.sh /path/to/ha/config [container_name]
# Adds config/packages/phonejack.yaml, enables packages in configuration.yaml
# (once, with a backup), checks the config and restarts the container.
set -e
CFG="${1:?usage: install_ha.sh /path/to/ha/config [container_name]}"
CT="${2:-homeassistant}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$CFG/packages"
cp "$HERE/phonejack_ha.yaml" "$CFG/packages/phonejack.yaml"
if ! grep -q "packages:" "$CFG/configuration.yaml"; then
  cp "$CFG/configuration.yaml" "$CFG/configuration.yaml.bak-phonejack"
  printf '\nhomeassistant:\n  packages: !include_dir_named packages\n' >> "$CFG/configuration.yaml"
fi
docker exec "$CT" python3 -m homeassistant --script check_config -c /config | tail -15
echo "Config check done. Restarting Home Assistant..."
docker restart "$CT"
