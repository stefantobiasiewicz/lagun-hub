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

echo "== permission to set the CAN interfaces up"
# The hub brings can0… to its entry in hub.yaml at every start (`ip link`, through
# `sudo -n`) — that is what makes starting it one command (zadania/FEAT-01.md, R3, R5).
# `ip link set` needs root, the hub runs as this user, so it gets exactly that and no
# more: a rule for `ip link set can*`. Not CAP_NET_ADMIN on the interpreter (every
# Python script would get it) and not a systemd unit (the hub runs in a shell).
#
# `can*` covers every argument after the name, so the rule also allows e.g. renaming a
# CAN interface — a known width, kept because a narrower sudoers pattern cannot say
# "only bitrate, restart-ms, fd, up and down". Virtual `vcan*` is NOT covered: creating
# one takes modprobe and `ip link add`, and the hub only checks it (R7).
#
# Checked with `visudo -c` before it goes in: a broken file in /etc/sudoers.d can lock
# sudo out for everyone on the machine.
IP="$(command -v ip || echo /usr/sbin/ip)"
RULE="$(id -un) ALL=(root) NOPASSWD: $IP link set can*"
CANDIDATE="$(mktemp)"
trap 'rm -f "$CANDIDATE"' EXIT
echo "$RULE" > "$CANDIDATE"
if sudo visudo -cqf "$CANDIDATE"; then
    sudo install -m 0440 -o root -g root "$CANDIDATE" /etc/sudoers.d/lagun-hub
    echo "   /etc/sudoers.d/lagun-hub: $RULE"
else
    echo "   visudo rejected the rule, nothing written: $RULE" >&2
    echo "   without it the hub starts, but cannot set can0 up — it says so in Issues" >&2
fi

cat <<NEXT_STEPS

Next, by hand:

  once                echo 'export LAGUN_CONFIG=$CONFIG' >> ~/.bashrc && exec bash

  the hub             .venv/bin/lagun-hub run
                      (it sets can0 up by itself — bitrate from hub.yaml)
  a virtual bus       sudo ./tools/can-up.sh vcan0          (trials; the hub only checks it)
  traffic, no hub     candump can0
  the view            .venv/bin/lagun-hub tui                (from a second terminal)

The hub holds the terminal. For longer work: tmux or screen.
The installation starts EMPTY — nodes come in once you open joining
with the [a] key in the view.
NEXT_STEPS
