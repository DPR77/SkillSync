#!/bin/sh
#
#  skill-sync launcher for macOS and Linux.
#
#  Finds a Python that actually runs, installs one if there is none and that can be done
#  without root, then runs a skill-sync script with it.
#
#      ./launch.sh                    opens the interactive menu
#      ./launch.sh sync.py status     runs that script with those arguments

set -u

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usable() {
    [ -n "${1:-}" ] || return 1
    command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

find_python() {
    for candidate in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                     /usr/bin/python3 "$HOME/.local/bin/python3"; do
        if usable "$candidate"; then
            PYEXE=$candidate
            return 0
        fi
    done
    return 1
}

PYEXE=
if ! find_python; then
    echo
    echo "  Python was not found, and skill-sync is written in Python."
    echo

    # Homebrew installs into the user's own prefix, so it is safe to run for them.
    # Anything needing root is only ever printed: a launcher should not be calling sudo,
    # and a password prompt in a spawned window is a hang waiting to happen.
    if [ "$(uname -s)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        echo "  > brew install python"
        brew install python || true
        find_python || true
    fi

    if [ -z "$PYEXE" ]; then
        if command -v apt-get >/dev/null 2>&1; then
            hint="sudo apt-get install -y python3"
        elif command -v dnf >/dev/null 2>&1; then
            hint="sudo dnf install -y python3"
        elif command -v pacman >/dev/null 2>&1; then
            hint="sudo pacman -S python"
        elif [ "$(uname -s)" = "Darwin" ]; then
            hint="install Homebrew from https://brew.sh, then: brew install python"
        else
            hint="install Python 3.8+ with your package manager"
        fi
        echo "  skill-sync cannot install Python here without root. Run this yourself:"
        echo "      $hint"
        echo
        exit 1
    fi
    echo
    echo "  Python is ready: $PYEXE"
    echo
fi

if [ $# -eq 0 ]; then
    exec "$PYEXE" "$HERE/menu.py"
fi

script=$1
shift
exec "$PYEXE" "$HERE/$script" "$@"
