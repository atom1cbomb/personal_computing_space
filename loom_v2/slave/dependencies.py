"""Private dependency snapshots; only this activation receives its credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path


def directory(settings, workspace, slave_id, digest, instance):
    return Path(settings.runtime_plugin_instance_state_dir) / workspace / slave_id / digest / instance / "dependencies"


def prepare(settings, workspace, activation):
    path = directory(settings, workspace, activation.target_slave, activation.package_digest, activation.instance_name)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.geteuid() == 0:
        os.chown(path, 65534, 65534)
    os.chmod(path, 0o700)
    return path


def write(settings, workspace, activation, snapshot):
    path = prepare(settings, workspace, activation)
    temporary = path / ".context.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(snapshot, stream, separators=(",", ":"))
    if os.geteuid() == 0:
        os.chown(temporary, 65534, 65534)
    os.chmod(temporary, 0o400)
    os.replace(temporary, path / "context.json")


def revoke(settings, workspace, activation):
    (directory(settings, workspace, activation.target_slave, activation.package_digest, activation.instance_name) / "context.json").unlink(missing_ok=True)
