#!/usr/bin/env bash
# Runs INSIDE a distro container (see ci.yml). The installer finds or installs the prerequisites the way
# it would on a real instance, then the tests run on that distro's own Python.
set -euo pipefail
cd /repo/src
bash install_guest.sh --deps-only
python3 -c 'import sys; print("python3 is", sys.version.split()[0]); assert sys.version_info >= (3, 6)'
python3 -I -S -c 'import sys; sys.path.insert(0, "."); import ec2_idle; print("ec2_idle imports the way the service runs it (-I -S)")'
cd /repo
PYTHONIOENCODING=utf-8 python3 -B -m unittest discover -s src
