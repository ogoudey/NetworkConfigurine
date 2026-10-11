#!/usr/bin/env python3
"""
netmon monitor — terminal dashboard for every node in network_config.json.

For each node it shows two status dots and the node's projects:
  ● 1st dot  ping       (green = replies, red = no reply, grey = ping unavailable)
  ● 2nd dot  server     (green = node_server.py answers /health, red = not)
    └─ ● project  version (branch) date      green = present, yellow = uncommitted
                                             changes, red = missing / not broadcast

  python3 monitor.py                         # live dashboard (uses ./network_config.json)
  python3 monitor.py path/to/network_config.json
  python3 monitor.py --once                  # print once and exit (exit 1 if a server is down)

Keys: q quit · r refresh now · ↑↓/PgUp/PgDn scroll.  The config file is
re-read automatically when it changes.  Standard library only; Python 3.7+.
"""
from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

DEFAULT_PORT = 8765
_opener = build_opener(ProxyHandler({}))  # never route LAN checks through an http_proxy


def _cursor(visible):
    try:
        curses.curs_set(visible)
    except curses.error:  # some terminals (e.g. vt100) can't hide the cursor
        pass


# ----------------------------------------------------------------------------- config

def load_network_config(path):
    raw = json.loads(Path(path).read_text())
    port = int(raw.get("default_port", DEFAULT_PORT))
    nodes = []
    for i, n in enumerate(raw.get("nodes", [])):
        if isinstance(n, str):
            n = {"host": n}
        n = dict(n)
        if "host" not in n:
            raise ValueError("node #%d has no 'host'" % (i + 1))
        n.setdefault("name", n["host"])
        n["port"] = int(n.get("port", port))
        n.setdefault("expected_projects", [])
        nodes.append(n)
    return {"nodes": nodes,
            "refresh": float(raw.get("refresh_seconds", 10)),
            "timeout": float(raw.get("timeout_seconds", 2))}


# ----------------------------------------------------------------------------- checks

_PING = shutil.which("ping")
_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.I)


def do_ping(host, timeout):
    """-> (ok: True/False/None, ms, detail).  None means ping isn't available."""
    if not _PING:
        return None, None, "n/a"
    t = str(max(1, int(round(timeout))))
    system = platform.system()
    if system == "Windows":
        cmd = [_PING, "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif system == "Darwin":
        cmd = [_PING, "-c", "1", "-t", t, host]
    else:
        cmd = [_PING, "-c", "1", "-W", t, host]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout + 2)
    except subprocess.TimeoutExpired:
        return False, None, "no reply"
    except OSError:
        return None, None, "n/a"
    ok = r.returncode == 0 and (system != "Windows" or "TTL=" in r.stdout.upper())
    if ok:
        m = _TIME_RE.search(r.stdout)
        return True, float(m.group(1)) if m else (time.monotonic() - t0) * 1000, None
    if r.returncode == 1 or system == "Windows":
        return False, None, "no reply"
    out = r.stdout.lower()
    if "unknown host" in out or "not known" in out or "cannot resolve" in out:
        return False, None, "unknown host"
    if "permitted" in out or "permission" in out:
        return None, None, "no permission"
    return False, None, "error"


def http_json(host, port, path, timeout):
    h = "[%s]" % host if ":" in host and not host.startswith("[") else host
    with _opener.open("http://%s:%d%s" % (h, port, path), timeout=timeout) as r:
        return json.loads(r.read().decode())


def short_err(e):
    if isinstance(e, HTTPError):
        return "HTTP %d" % e.code
    r = getattr(e, "reason", e)
    if isinstance(r, ConnectionRefusedError):
        return "refused — server not running?"
    if isinstance(r, socket.gaierror):
        return "unknown host"
    if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in str(r):
        return "timeout"
    if isinstance(e, ValueError):
        return "bad response"
    if isinstance(r, OSError) and r.strerror:
        return r.strerror.lower()
    return str(r)[:40]


def check_node(node, timeout):
    res = {"checked_at": time.time()}
    res["ping"], res["ping_ms"], res["ping_detail"] = do_ping(node["host"], timeout)
    try:
        res["health"] = http_json(node["host"], node["port"], "/health", timeout)
        res["http"] = True
    except Exception as e:
        res["http"], res["http_detail"] = False, short_err(e)
        return res
    try:
        res["projects"] = http_json(node["host"], node["port"], "/projects", timeout + 8)["projects"]
    except Exception as e:
        res["projects_err"] = short_err(e)
    return res


class Poller(threading.Thread):
    """Re-checks all nodes in parallel every `refresh` seconds (or when woken)."""

    def __init__(self, cfg_path):
        super().__init__(daemon=True)
        self.cfg_path = Path(cfg_path)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.states, self.busy, self.last_cycle, self.error = {}, False, None, None
        self._mtime = None
        self.cfg = {"nodes": [], "refresh": 10, "timeout": 2}
        self.reload()
        self.pool = ThreadPoolExecutor(max_workers=32)

    def reload(self):
        try:
            mtime = self.cfg_path.stat().st_mtime
            if mtime != self._mtime:
                cfg = load_network_config(self.cfg_path)
                with self.lock:
                    self.cfg, self.states, self._mtime, self.error = cfg, {}, mtime, None
        except Exception as e:
            self.error = "config error: %s" % e

    def run(self):
        while True:
            self.reload()
            nodes, timeout = self.cfg["nodes"], self.cfg["timeout"]
            self.busy = True
            futs = {self.pool.submit(check_node, n, timeout): i for i, n in enumerate(nodes)}
            for f in as_completed(futs):
                try:
                    res = f.result()
                except Exception as e:
                    res = {"ping": None, "http": False, "http_detail": "check failed: %s" % e}
                with self.lock:
                    if self.cfg["nodes"] is nodes:
                        self.states[futs[f]] = res
            self.busy, self.last_cycle = False, time.time()
            self.wake.wait(self.cfg["refresh"])
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            return self.cfg, dict(self.states)


# ----------------------------------------------------------------------------- rendering (shared by curses + plain text)

OK, BAD, WARN, DIM, BOLD, NORM, HEAD = "ok", "bad", "warn", "dim", "bold", "norm", "head"
DOT, HOLLOW = "●", "○"


def dot(v):
    if v is True:
        return (DOT, OK)
    if v is False:
        return (DOT, BAD)
    if v == "pending":
        return (DOT, WARN)
    return (HOLLOW, DIM)


def human(sec):
    sec = int(sec)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            return "%d%s" % (sec // n, unit)
    return "%ds" % sec


def build_lines(cfg, states):
    nodes = cfg["nodes"]
    if not nodes:
        return [[("  No nodes in config.", DIM)]]
    hostport = ["%s:%d" % (n["host"], n["port"]) for n in nodes]
    nw = max([len(n["name"]) for n in nodes] + [4])
    hw = max([len(h) for h in hostport] + [4])
    allp = [p for s in states.values() for p in (s.get("projects") or [])]
    pw = max([len(p.get("name", "")) for p in allp] + [len(e) for n in nodes for e in n["expected_projects"]] + [8])
    vw = max([len(p.get("version") or "") for p in allp] + [8])

    lines = [[("  P S  ", HEAD), ("NODE".ljust(nw) + "  ", HEAD), ("HOST".ljust(hw) + "  ", HEAD),
              ("PING".ljust(10), HEAD), ("SERVER", HEAD)], []]
    for i, node in enumerate(nodes):
        st = states.get(i)
        pend = st is None
        ping = "pending" if pend else st.get("ping")
        http = "pending" if pend else st.get("http")
        if pend:
            ptxt, pcol = "…", DIM
        elif ping is True:
            ptxt, pcol = ("%.2f ms" if st["ping_ms"] < 1 else "%.1f ms") % st["ping_ms"], NORM
        else:
            ptxt, pcol = st.get("ping_detail") or "n/a", (BAD if ping is False else DIM)
        row = [("  ", NORM), dot(ping), (" ", NORM), dot(http), ("  ", NORM),
               (node["name"].ljust(nw) + "  ", BOLD), (hostport[i].ljust(hw) + "  ", NORM),
               (ptxt.ljust(10), pcol)]
        if pend:
            row.append(("checking…", DIM))
        elif http:
            h = st["health"]
            row.append(("ok", OK))
            row.append(("  up %s" % human(h.get("uptime_s", 0)), DIM))
            if h.get("node") and h["node"] != node["name"]:
                row.append(("  (calls itself '%s')" % h["node"], DIM))
        else:
            row.append((st.get("http_detail", "down"), BAD))
        lines.append(row)

        # project rows
        sub = []
        if not pend and http:
            projects = st.get("projects")
            if projects is None:
                sub.append([("project list failed: %s" % st.get("projects_err"), BAD)])
            else:
                names = {p.get("name") for p in projects}
                for p in projects:
                    if not p.get("exists"):
                        sub.append([dot(False), (" " + p["name"].ljust(pw) + "  ", NORM),
                                    ("missing on disk: %s" % p.get("path"), BAD)])
                        continue
                    g = p.get("git") or {}
                    seg = [dot(True) if not g.get("dirty") else (DOT, WARN), (" " + p["name"].ljust(pw) + "  ", NORM),
                           ((p.get("version") or "?").ljust(vw), BOLD)]
                    if g.get("branch"):
                        seg.append(("  " + g["branch"], DIM))
                    if g.get("date"):
                        seg.append(("  " + g["date"], DIM))
                    if g.get("dirty"):
                        seg.append(("  uncommitted changes", WARN))
                    if not g:
                        seg.append(("  (no git)", DIM))
                    sub.append(seg)
                for e in node["expected_projects"]:
                    if e not in names:
                        sub.append([dot(False), (" " + e.ljust(pw) + "  ", NORM), ("not broadcast by node", BAD)])
                if not sub:
                    sub.append([("(no projects configured — run node_setup.py on this node)", DIM)])
        elif not pend and node["expected_projects"]:
            for e in node["expected_projects"]:
                sub.append([dot(None), (" " + e.ljust(pw) + "  ", DIM), ("unknown (server unreachable)", DIM)])
        for j, seg in enumerate(sub):
            lines.append([(" " * 7 + ("└─ " if j == len(sub) - 1 else "├─ "), DIM)] + seg)
        lines.append([])
    return lines


def title_segs(cfg_path, cfg, states, poller=None):
    nodes = cfg["nodes"]
    up = sum(1 for s in states.values() if s.get("http"))
    reach = sum(1 for s in states.values() if s.get("ping") or s.get("http"))
    segs = [(" netmon ", HEAD), (str(cfg_path), DIM),
            ("   %d/%d reachable · %d/%d servers up" % (reach, len(nodes), up, len(nodes)),
             OK if nodes and up == len(nodes) else WARN)]
    if poller:
        if poller.error:
            segs.append(("   " + poller.error, BAD))
        elif poller.busy:
            segs.append(("   checking…", DIM))
        elif poller.last_cycle:
            segs.append(("   checked %s · every %gs" % (time.strftime("%H:%M:%S", time.localtime(poller.last_cycle)),
                                                       cfg["refresh"]), DIM))
    return segs


# ----------------------------------------------------------------------------- plain-text mode

ANSI = {OK: "\033[32m", BAD: "\033[31m", WARN: "\033[33m", DIM: "\033[2m",
        BOLD: "\033[1m", HEAD: "\033[1;36m", NORM: ""}


def run_once(cfg_path, color):
    cfg = load_network_config(cfg_path)
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda n: check_node(n, cfg["timeout"]), cfg["nodes"]))
    states = dict(enumerate(results))

    def fmt(segs):
        if color:
            return "".join(ANSI[s] + t + ("\033[0m" if ANSI[s] else "") for t, s in segs)
        return "".join(t for t, _ in segs)

    print(fmt(title_segs(cfg_path, cfg, states)))
    print()
    for segs in build_lines(cfg, states):
        print(fmt(segs).rstrip())
    return 0 if all(s.get("http") for s in results) else 1


# ----------------------------------------------------------------------------- curses mode

def run_tui(cfg_path):
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    poller = Poller(cfg_path)
    poller.start()

    def _main(scr):
        _cursor(0)
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = 0
        pairs = {OK: curses.COLOR_GREEN, BAD: curses.COLOR_RED, WARN: curses.COLOR_YELLOW, HEAD: curses.COLOR_CYAN}
        attr = {DIM: curses.A_DIM, BOLD: curses.A_BOLD, NORM: 0}
        if curses.has_colors():
            for i, (k, c) in enumerate(pairs.items(), 1):
                curses.init_pair(i, c, bg)
                attr[k] = curses.color_pair(i) | (curses.A_BOLD if k in (HEAD,) else 0)
        else:  # monochrome terminal
            attr.update({OK: curses.A_BOLD, BAD: curses.A_REVERSE, WARN: curses.A_UNDERLINE, HEAD: curses.A_BOLD})
        scr.timeout(250)
        scroll = 0

        def draw_row(y, segs, w):
            x = 0
            for text, style in segs:
                if x >= w - 1:
                    break
                t = text[: w - 1 - x]
                try:
                    scr.addstr(y, x, t, attr[style])
                except curses.error:
                    pass
                x += len(t)

        while True:
            cfg, states = poller.snapshot()
            lines = build_lines(cfg, states)
            h, w = scr.getmaxyx()
            body_h = max(1, h - 3)
            scroll = max(0, min(scroll, len(lines) - body_h))
            scr.erase()
            draw_row(0, title_segs(cfg_path, cfg, states, poller), w)
            for y, segs in enumerate(lines[scroll:scroll + body_h]):
                draw_row(y + 2, segs, w)
            more = " (more ↓)" if scroll + body_h < len(lines) else ""
            draw_row(h - 1, [(" q quit · r refresh now · ↑↓ scroll" + more, DIM)], w)
            scr.refresh()

            try:
                k = scr.getch()
            except KeyboardInterrupt:
                return
            if k in (ord("q"), ord("Q"), 27):
                return
            if k in (ord("r"), ord("R")):
                poller.wake.set()
            elif k in (curses.KEY_UP, ord("k")):
                scroll -= 1
            elif k in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif k == curses.KEY_PPAGE:
                scroll -= body_h
            elif k == curses.KEY_NPAGE:
                scroll += body_h

    curses.wrapper(_main)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live status of the nodes in network_config.json")
    ap.add_argument("config", nargs="?", default="subnets/network_config.json", help="default: %(default)s")
    ap.add_argument("--once", action="store_true", help="check once, print, and exit")
    ap.add_argument("--no-color", action="store_true", help="plain text output with --once")
    args = ap.parse_args(argv)
    if not Path(args.config).is_file():
        sys.exit("Config not found: %s  (see network_config.example.json)" % args.config)
    try:
        load_network_config(args.config)
    except Exception as e:
        sys.exit("Bad config %s: %s" % (args.config, e))
    if args.once or not sys.stdout.isatty():
        return run_once(args.config, color=sys.stdout.isatty() and not args.no_color)
    run_tui(args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
