# © 2026 PocketKode. All rights reserved.
"""Wi-Fi Transfer in the background: the transfer page keeps working while muOS is back in its menu.

The app hands over (X on its screen): it saves the PIN, the signed-in browsers and the port in data/background.json,
closes its own server and starts `main.py --background` in a session of its own, then quits. This process runs the
same server without a screen, keeps the game console awake while files move, and stops after the usual idle time.
Opening Wi-Fi Transfer again stops it (SIGTERM) and takes the PIN and the signed-in browsers back, so the page
doesn't have to log in again."""
import json
import os
import signal
import socket
import sys
import time

PID_FILE = "/tmp/wifitransfer-background.pid"


def state_path(app_dir):
    return os.path.join(app_dir, "data", "background.json")


def save_state(app_dir, server):
    os.makedirs(os.path.join(app_dir, "data"), exist_ok=True)
    with server.lock:
        state = {"pin": server.pin, "tokens": sorted(server.tokens), "port": server.port, "saved": time.time()}
    tmp = state_path(app_dir) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path(app_dir))


def load_state(app_dir, max_age=6 * 3600):
    """The PIN, signed-in browsers and port handed over, if recent; else None."""
    try:
        with open(state_path(app_dir)) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or time.time() - float(state.get("saved") or 0) > max_age:
        return None
    pin = str(state.get("pin") or "")
    if not (len(pin) == 4 and pin.isdigit()):
        return None
    return {"pin": pin, "tokens": [str(t) for t in state.get("tokens") or []], "port": int(state.get("port") or 8080)}


def apply_state(server, state):
    with server.lock:
        server.pin = state["pin"]
        server.tokens = set(state["tokens"])


def running_pid():
    """The background process's pid if it's running, else None."""
    try:
        with open(PID_FILE) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def stop_running(timeout=8.0):
    """Stops the background process (if any) and waits until it has let go of the port. True if one was running."""
    pid = running_pid()
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return True


def start(app_dir):
    """Starts `main.py --background` on its own (it outlives the app and isn't stopped with it)."""
    import subprocess
    os.makedirs(os.path.join(app_dir, "logs"), exist_ok=True)
    log = open(os.path.join(app_dir, "logs", "background.log"), "ab")
    subprocess.Popen([sys.executable, "-u", os.path.join(app_dir, "main.py"), "--background"], cwd=app_dir,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                     close_fds=True, env=dict(os.environ))


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run(app_dir):
    """`main.py --background`: the transfer server without a screen. Returns the exit code."""
    from .settings import load_config, make_roots
    from .keepawake import KeepAwake
    from .server import TransferServer

    state = load_state(app_dir)
    if not state:
        print("[background] nothing handed over: not starting", flush=True)
        return 1
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # muOS going back to its menu doesn't stop it

    cfg = load_config(app_dir)
    server = TransferServer(make_roots(cfg), os.path.join(app_dir, "web"), port=state["port"])
    end = time.monotonic() + 15
    while not _port_free(state["port"]) and time.monotonic() < end:  # the app is still letting go of it
        time.sleep(0.2)
    server.start()
    apply_state(server, state)
    server.event("Running in the background")
    print(f"[background] serving on port {server.port}", flush=True)

    awake = KeepAwake()
    idle = float(cfg.get("idle_minutes") or 15) * 60
    reason = "stopped"
    try:
        while not stop["now"]:
            if server.busy() or server.connected():
                awake.start()
                awake.pulse()
            else:
                awake.stop()
            if not server.busy() and time.monotonic() - server.last_activity > idle:
                reason = "idle"
                break
            time.sleep(0.5)
    finally:
        save_state(app_dir, server)  # the app takes the PIN and the signed-in browsers back
        server.stop()
        awake.stop()
        if hasattr(os, "sync"):
            os.sync()
        try:
            if running_pid() == os.getpid():
                os.remove(PID_FILE)
        except OSError:
            pass
        print(f"[background] {reason}", flush=True)
    return 0
