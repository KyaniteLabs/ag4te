#!/usr/bin/env bash
set -euo pipefail

case "$#:${1:-}" in
  1:check|1:record|2:render) ;;
  *) echo "Usage: $0 check | record | render NEW_DIRECTORY" >&2; exit 2 ;;
esac

root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
plugin="$root/plugins/devarch-hermes"
patch="$root/scripts/devarch-vendor.patch"
source_sha=$(sed -n 's/^PINNED_SHA = "\([0-9a-f]*\)"$/\1/p' "$plugin/__init__.py")
if [[ ! $source_sha =~ ^[0-9a-f]{40}$ ]]; then
  echo "Cannot read the vendor source pin from $plugin/__init__.py" >&2
  exit 1
fi

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
mkdir "$scratch/base"
git -C "$root" archive "$source_sha" archaeology |
  tar -xf - -C "$scratch/base" \
    --exclude=archaeology/mcp_server --exclude=archaeology/templates \
    --exclude=archaeology/visualization/template.html \
    --exclude=archaeology/visualization/global-template.html \
    --exclude=archaeology/visualization/multi-project-dashboard.html

snapshot_vendor() {
  mkdir "$scratch/local"
  tar -cf - -C "$plugin" --exclude=__pycache__ --exclude='*.pyc' --exclude=.DS_Store archaeology |
    tar -xf - -C "$scratch/local"
}

apply_vendor_patch() {
  if [[ -s $patch ]]; then
    GIT_CEILING_DIRECTORIES="$(cd "$(dirname "$1")" && pwd -P)" git -C "$1" apply -p2 "$patch"
  fi
}

case "$1" in
  record)
    snapshot_vendor
    status=0
    git -C "$scratch" diff --no-index --binary --full-index --no-renames \
      -- base/archaeology local/archaeology || status=$?
    [[ $status -le 1 ]]
    ;;
  check)
    apply_vendor_patch "$scratch/base"
    snapshot_vendor
    git -C "$scratch" diff --no-index --exit-code -- base/archaeology local/archaeology
    echo "Vendored DevArch matches $source_sha plus the recorded patch."
    ;;
  render)
    mkdir -- "$2"
    cp -R "$scratch/base/archaeology" "$2/"
    if ! apply_vendor_patch "$2"; then
      echo "Patch needs reconciliation. The source tree is available at $2/archaeology." >&2
      exit 1
    fi
    ;;
esac
