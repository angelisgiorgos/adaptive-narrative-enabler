#!/bin/sh
# Docker copies the image's config into a named volume only when the volume is
# first created, so config files added in later releases (e.g. events.yaml,
# missions.yaml) would never reach an existing volume. Seed any missing default
# file on every start; existing files, including edited ones, are never replaced.
# Then add, once, each default entry (place, NPC, item, event, mission) that the
# volume's files lack: a release's new entries refer to each other (a new mission
# names its new NPC), and an older file next to a newer one would otherwise make a
# world that cannot be played. An entry deleted afterwards stays deleted.
set -e

defaults="${ANE_CONFIG_DEFAULTS:-/app/config.defaults}"
target="${ANE_DATA_DIR:-/app/config}"

if [ -d "$defaults" ] && [ -d "$target" ]; then
    for source in "$defaults"/*.yaml; do
        [ -e "$source" ] || continue
        destination="$target/$(basename "$source")"
        if [ ! -e "$destination" ]; then
            if cp "$source" "$destination"; then
                echo "[config] Added missing default config file: $(basename "$source")"
            else
                echo "[config] Could not add $(basename "$source") to $target (read-only?)" >&2
            fi
        fi
    done
    if ! python -m utils.config_loader upgrade "$defaults" "$target"; then
        echo "[config] Could not add the new default entries to $target" >&2
    fi
fi

exec "$@"
