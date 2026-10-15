#!/usr/bin/env python3
"""Recover only outlook-mail-local after sustained Docker health failures."""
import json
import os
from pathlib import Path
import subprocess
import time

CONTAINER = "outlook-mail-local"
CONFIRM_SECONDS = 60
COOLDOWN_SECONDS = 600


def docker(*args):
    return subprocess.check_output(["/usr/bin/docker", *args], text=True, stderr=subprocess.DEVNULL, timeout=50)


def inspect_container():
    info = json.loads(docker("inspect", CONTAINER))[0]
    state = info["State"]
    return {"id": info["Id"], "running": state.get("Running", False),
            "health": state.get("Health", {}).get("Status", "none"),
            "checks": [{"start": item.get("Start"), "end": item.get("End"), "exit": item.get("ExitCode")}
                       for item in state.get("Health", {}).get("Log", [])]}


def emit(event, **values):
    print(json.dumps({"event": event, "container": CONTAINER, **values}), flush=True)


def check_once(path, now=None):
    now = time.time() if now is None else now
    # Failed inspection must not trigger a restart or reset a previous cooldown.
    current = inspect_container()
    state = json.loads(path.read_text()) if path.exists() else {}
    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        os.replace(temporary, path)

    if not current["running"] or current["health"] != "unhealthy":
        if state.pop("unhealthy_id", None):
            emit("health_changed", health=current["health"], running=current["running"])
        state.pop("unhealthy_since", None)
        save()
        return
    if state.get("unhealthy_id") != current["id"]:
        state.update(unhealthy_id=current["id"], unhealthy_since=now)
        save()
        emit("unhealthy_observed", id=current["id"], checks=current["checks"])
        return
    if now - state["unhealthy_since"] < CONFIRM_SECONDS:
        return
    if now - state.get("last_attempt", -COOLDOWN_SECONDS) < COOLDOWN_SECONDS:
        return
    # A concurrent operator may stop or replace the container while we inspect it.
    latest = inspect_container()
    if latest["id"] != current["id"] or not latest["running"] or latest["health"] != "unhealthy":
        return
    state["last_attempt"] = now
    save()  # Persist before restart, including failed/timeout attempts.
    emit("restart_attempt", id=current["id"], checks=latest["checks"], load=os.getloadavg())
    try:
        emit("process_snapshot", processes=docker("top", current["id"], "-eo", "pid,ppid,comm,nlwp"))
    except (subprocess.SubprocessError, OSError):
        emit("process_snapshot_unavailable")
    docker("restart", "--time", "20", current["id"])
    state.pop("unhealthy_id", None)
    state.pop("unhealthy_since", None)
    save()
    emit("restart_completed", id=current["id"])


if __name__ == "__main__":
    try:
        check_once(Path("/var/lib/outlook-mail-watchdog/state.json"))
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        emit("check_failed", error_type=type(error).__name__)
        raise SystemExit(1)
