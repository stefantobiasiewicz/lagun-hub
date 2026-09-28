#!/usr/bin/env bash
# Preparing a clone for work on a Raspberry Pi. Run it FROM THE CLONE'S DIRECTORY:
#
#   git clone <repo> ~/hub && cd ~/hub && ./tools/setup-rpi.sh
#
# The hub then runs IN A SHELL, from this directory — not as a service. At this stage a
# service would be ceremony around something that changes every little while anyway.
#
# The script is idempotent: it may be run a second time after `git pull`.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f pyproject.toml ]] || { echo "this is not a hub clone" >&2; exit 1; }

echo "== system packages"
# python3-venv for the environment, can-utils for candump — without it there is no way
# to check whether anything travels the bus at all before the hub gets into it.
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev can-utils

echo "== python environment"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
# Editable: `git pull` is enough, there is no need to install again.
.venv/bin/pip install --quiet -e ".[dev]"

echo "== the installation's configuration"
# OUTSIDE the clone, and that is the point: `config/` in the repository is an example to
# read — it holds two Uids that exist nowhere but in the tests. A real house starts
# empty, and `git pull` has nothing to collide with.
CONFIG="${LAGUN_CONFIG:-$HOME/lagun}"
if [[ -f "$CONFIG/hub.yaml" ]]; then
    echo "   keeping the one in $CONFIG"
else
    .venv/bin/lagun-hub init "$CONFIG" --bus can0
fi

echo
LAGUN_CONFIG="$CONFIG" .venv/bin/lagun-hub doctor || true

cat <<NEXT_STEPS

Next, by hand:

  once                echo 'export LAGUN_CONFIG=$CONFIG' >> ~/.bashrc && exec bash

  the bus             sudo ./tools/can-up.sh can0 500000
                      sudo ./tools/can-up.sh vcan0          (virtual, for trials)

  the hub             .venv/bin/lagun-hub run
  traffic, no hub     candump can0
  the view            .venv/bin/lagun-hub tui                (from a second terminal)

The hub holds the terminal. For longer work: tmux or screen.
The installation starts EMPTY — nodes come in once you open joining
with the [a] key in the view.
NEXT_STEPS
