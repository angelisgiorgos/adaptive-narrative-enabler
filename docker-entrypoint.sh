#!/bin/sh
# Docker copies the image's config into a named volume only when the volume is
# first created, so config files added in later releases (e.g. events.yaml,
# missions.yaml) would never reach an existing volume. Seed any missing default
# file on every start; existing files are never replaced whole. Then bring the
# volume up to date (utils/config_loader.upgrade_config_dir): what it lacks is
# added (a default entry once: one deleted afterwards stays deleted), and entries
# and settings nobody edited (a value an earlier release had) take this release's
# default; edited ones are kept, unless ANE_CONFIG_TAKE_DEFAULTS names them
# (comma-separated names or setting paths, or `all`). Changed files are backed up
# in .ane-backups/ first.
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
    if ! python -m utils.config_loader upgrade "$defaults" "$target" --take-defaults "${ANE_CONFIG_TAKE_DEFAULTS:-}"; then
        echo "[config] Could not bring $target up to date with this release's defaults" >&2
    fi
fi

exec "$@"
