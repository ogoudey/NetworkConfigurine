#!/usr/bin/env python3
"""
netmon node server — the tiny HTTP status server that runs ON each node.

Endpoints (GET, JSON):
  /health            {"status": "ok", "node": ..., "uptime_s": ..., ...}
  /projects          {"node": ..., "projects": [ {name, path, exists, version, git, manifest}, ... ]}
  /projects/<name>   one project

Usage (on the node):
  python3 node_server.py start     # run in background (survives ssh logout)
  python3 node_server.py stop
  python3 node_server.py status
  python3 node_server.py run       # run in foreground (Ctrl-C to stop)

The config (default ~/.netmon/node.json) is written by node_setup.py and is
re-read on every request, so project changes apply without a restart.
Standard library only; Python 3.7+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from urllib.request import ProxyHandler, build_opener

NETMON_VERSION = "0.1.0"
DEFAULT_PORT = 8765
NETMON_DIR = Path.home() / ".netmon"
DEFAULT_CONFIG = Path(os.environ.get("NETMON_NODE_CONFIG", str(NETMON_DIR / "node.json"))).expanduser()
PID_FILE = NETMON_DIR / "server.pid"
LOG_FILE = NETMON_DIR / "server.log"
STARTED = time.time()


# ----------------------------------------------------------------------------- config

def load_config(path=DEFAULT_CONFIG) -> dict:
    """Load the node config, filling in defaults. Missing file -> empty config."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        data = {}
    data.setdefault("node_name", socket.gethostname())
    data.setdefault("port", DEFAULT_PORT)
    data.setdefault("projects", [])  # [{"name": ..., "path": ...}, ...]
    return data


def save_config(cfg: dict, path=DEFAULT_CONFIG) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)


# ----------------------------------------------------------------------------- version detection

def _git(path, *args):
    """Run a git command in `path`; return stripped stdout or None on failure."""
    try:
        r = subprocess.run(
            # safe.directory=* lets the server read repos owned by another user
            ["git", "-c", "safe.directory=*", "-C", str(path)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def git_info(path):
    if _git(path, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    commit = _git(path, "rev-parse", "--short", "HEAD")
    if not commit:
        return {"commit": None, "note": "no commits yet"}
    describe = _git(path, "describe", "--tags", "--always", "--dirty")
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "commit": commit,
        "describe": describe,                                   # e.g. v1.2.0-3-gabc1234-dirty
        "tag": _git(path, "describe", "--tags", "--abbrev=0"),  # nearest tag, or None
        "branch": None if branch == "HEAD" else branch,         # None when detached
        "date": _git(path, "log", "-1", "--format=%cd", "--date=short"),
        "dirty": bool(describe and describe.endswith("-dirty")),
    }


_TOML_RE = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.M)
_CFG_RE = re.compile(r'^\s*version\s*=\s*(\S+)', re.M)
_PY_RE = re.compile(r'''\bversion\s*=\s*["']([^"']+)["']''')


def _read(f: Path) -> str:
    try:
        with f.open(errors="replace") as fh:
            return fh.read(200_000)
    except OSError:
        return ""


def manifest_version(path):
    """Best-effort version from files in the project dir (no git needed)."""
    p = Path(path)
    f = p / "package.json"
    if f.is_file():
        try:
            v = json.loads(_read(f)).get("version")
        except (ValueError, AttributeError):
            v = None
        if v:
            return {"version": str(v), "source": "package.json"}
    for name, rx in (("pyproject.toml", _TOML_RE), ("Cargo.toml", _TOML_RE),
                     ("setup.cfg", _CFG_RE), ("setup.py", _PY_RE)):
        f = p / name
        if f.is_file():
            m = rx.search(_read(f))
            if m and not m.group(1).startswith("attr:"):
                return {"version": m.group(1), "source": name}
    for name in ("VERSION", "version.txt", ".version"):
        f = p / name
        if f.is_file():
            lines = _read(f).strip().splitlines()
            if lines and lines[0].strip():
                return {"version": lines[0].strip()[:40], "source": name}
    return None


def display_version(git, manifest) -> str:
    commit = git.get("commit") if git else None
    if git and git.get("tag") and git.get("describe"):
        return git["describe"]                      # v1.2.0  /  v1.2.0-3-gabc1234-dirty
    if manifest:
        return manifest["version"] + (" @" + commit if commit else "")
    if commit:
        return "@" + commit
    return "unversioned"


def project_status(entry: dict) -> dict:
    path = Path(entry["path"]).expanduser()
    out = {"name": entry.get("name") or path.name, "path": str(path), "exists": path.is_dir()}
    if not out["exists"]:
        out["version"] = None
        return out
    g, m = git_info(path), manifest_version(path)
    out.update(git=g, manifest=m, version=display_version(g, m))
    return out


# ----------------------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "netmon-node/" + NETMON_VERSION

    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = unquote(self.path.split("?", 1)[0]).rstrip("/") or "/"
        try:
            cfg = load_config(self.server.config_path)
        except Exception as e:  # bad JSON etc. — still answer, health shows the problem
            return self._send(500, {"status": "error", "error": "config: %s" % e})

        if route in ("/", "/health"):
            return self._send(200, {
                "status": "ok",
                "node": cfg["node_name"],
                "hostname": socket.gethostname(),
                "port": self.server.server_address[1],
                "uptime_s": int(time.time() - STARTED),
                "project_count": len(cfg["projects"]),
                "netmon_version": NETMON_VERSION,
            })
        if route == "/projects":
            return self._send(200, {"node": cfg["node_name"],
                                    "projects": [project_status(p) for p in cfg["projects"]]})
        if route.startswith("/projects/"):
            name = route[len("/projects/"):]
            for p in cfg["projects"]:
                if (p.get("name") or Path(p["path"]).name) == name:
                    return self._send(200, project_status(p))
            return self._send(404, {"error": "no project named %r" % name})
        # Future: per-project actions (e.g. POST /projects/<name>/run) hook in here.
        return self._send(404, {"error": "unknown route", "routes": ["/health", "/projects", "/projects/<name>"]})

    def log_message(self, fmt, *args):
        if not self.server.quiet:
            sys.stderr.write("%s %s - %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                               self.client_address[0], fmt % args))


# ----------------------------------------------------------------------------- process helpers

_no_proxy = build_opener(ProxyHandler({}))


def local_health(port, timeout=0.7):
    """Return the /health dict of a server on localhost:port, or None."""
    try:
        with _no_proxy.open("http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def running_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def cmd_run(args):
    cfg = load_config(args.config)
    port = args.port or cfg["port"]
    httpd = ThreadingHTTPServer((args.host, port), Handler)
    httpd.daemon_threads = True
    httpd.config_path = args.config
    httpd.quiet = args.quiet
    NETMON_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("netmon node server '%s' listening on %s:%d (config: %s, %d projects)"
          % (cfg["node_name"], args.host, port, args.config, len(cfg["projects"])), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if running_pid() == os.getpid():
            PID_FILE.unlink()
    return 0


def cmd_start(args):
    pid = running_pid()
    if pid:
        print("already running (pid %d). Use 'stop' first to restart." % pid)
        return 0
    NETMON_DIR.mkdir(parents=True, exist_ok=True)
    port = args.port or load_config(args.config)["port"]
    cmd = [sys.executable, os.path.abspath(__file__), "run", "--config", str(args.config),
           "--host", args.host, "--port", str(port)]
    with open(str(LOG_FILE), "ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(Path.home()))
    for _ in range(30):
        time.sleep(0.1)
        if proc.poll() is not None:
            break
        if local_health(port, 0.3):
            print("started (pid %d) on port %d — log: %s" % (proc.pid, port, LOG_FILE))
            return 0
    print("server failed to start; last log lines:")
    print("\n".join(_read(LOG_FILE).splitlines()[-8:]))
    return 1


def cmd_stop(args):
    pid = running_pid()
    if not pid:
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        if running_pid() != pid:
            break
    print("stopped (pid %d)" % pid)
    return 0


def cmd_status(args):
    pid = running_pid()
    port = args.port or load_config(args.config)["port"]
    h = local_health(port)
    if h:
        print("running (pid %s) on port %d — node '%s', %d projects, up %ds"
              % (pid or "?", port, h["node"], h["project_count"], h["uptime_s"]))
        return 0
    print("not responding on port %d%s" % (port, " (pid %d exists)" % pid if pid else ""))
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="netmon node status server")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "start", "stop", "status"])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="node config (default: %(default)s)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: %(default)s)")
    ap.add_argument("--port", type=int, default=None, help="override the port from the config")
    ap.add_argument("--quiet", action="store_true", help="don't log requests")
    args = ap.parse_args(argv)
    args.config = args.config.expanduser()
    return {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
