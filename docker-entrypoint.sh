#!/bin/sh
# Docker copies the image's config into a named volume only when the volume is
# first created, so config files added in later releases (e.g. events.yaml,
# missions.yaml) would never reach an existing volume. Seed any missing default
# file on every start; existing files, including edited ones, are never touched.
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
fi

exec "$@"
