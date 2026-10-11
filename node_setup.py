#!/usr/bin/env python3
"""
netmon node setup — terminal UI, run ON the node (typically over ssh), to choose
which project folders this node broadcasts, then start its status server.

  cd ~/netmon && python3 node_setup.py
  python3 node_setup.py --start-dir ~/Projects     # open the folder browser here

Everything works with the arrow keys and Enter:
  main screen     ↑↓ move · Enter on a project (remove) or on an action
                  (Add a project… / Save / Save & start server / Quit)
  folder browser  ↑↓ move · Enter/→ open folder · ←/Backspace go up
                  Enter on "✓ Add this folder" picks the folder you're in

Non-interactive (scriptable) variants:
  python3 node_setup.py --list
  python3 node_setup.py --add ~/Projects/foo ~/Desktop/bar --remove oldproj --name pi --port 8765

Writes ~/.netmon/node.json (override with --config or $NETMON_NODE_CONFIG).
Needs node_server.py in the same folder. Standard library only.
"""
from __future__ import annotations

import argparse
import curses
import locale
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import node_server as ns  # noqa: E402

SERVER_SCRIPT = str(HERE / "node_server.py")
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
BACK_KEYS = (curses.KEY_BACKSPACE, 127, 8)

# Files that make a folder look like a project (shown as a hint in the browser).
PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "setup.py", "Cargo.toml",
                   "go.mod", "CMakeLists.txt", "Makefile", "VERSION")


def _cursor(visible):
    try:
        curses.curs_set(visible)
    except curses.error:  # some terminals (e.g. vt100) can't hide the cursor
        pass


def tilde(p):
    p, home = str(p), str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


# ----------------------------------------------------------------------------- project setup hook

def setup_project(entry: dict, ui: "SetupTUI"):
    """Project-specific setup, run right after a project is picked in the browser.

    For now a project only needs to exist, so this accepts it as-is. Later this is
    where to ask what the node should do with the project (e.g. a command to run)
    and store the answers as extra keys on `entry`; extra keys are preserved in
    ~/.netmon/node.json and passed to node_server.py. `ui.choose(...)` and
    `ui.prompt(...)` are available for asking questions. Return None to cancel.
    """
    return entry


# ----------------------------------------------------------------------------- model

class Item:
    def __init__(self, entry):
        self.entry = dict(entry)    # keeps any extra/future fields
        self.path = str(Path(entry["path"]).expanduser())
        self.name = entry.get("name") or Path(self.path).name
        self._version = None

    @property
    def version(self):
        if self._version is None:
            if not Path(self.path).is_dir():
                self._version = "missing on disk"
            else:
                st = ns.project_status({"path": self.path})
                g = st.get("git") or {}
                self._version = st["version"] + ("  (%s)" % g["branch"] if g.get("branch") else "")
        return self._version

    def to_entry(self):
        e = dict(self.entry)
        e.update(name=self.name, path=self.path)
        return e


def project_marker(path):
    for m in PROJECT_MARKERS:
        if os.path.exists(os.path.join(str(path), m)):
            return "git" if m == ".git" else m
    return None


def list_dirs(path, show_hidden):
    out = []
    with os.scandir(str(path)) as it:  # may raise PermissionError
        for e in it:
            if not show_hidden and e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    out.append(e.name)
            except OSError:
                pass
    return sorted(out, key=str.lower)


# ----------------------------------------------------------------------------- TUI

ACTIONS = (("add", "+ Add a project…"), ("save", "Save"), ("start", "Save & start server"), ("quit", "Quit"))
MAIN_HELP = "↑↓ move · Enter select · a add · d remove · n name · p port · s save · S save+start · q quit"


class SetupTUI:
    def __init__(self, stdscr, cfg, cfg_path, start_dir):
        self.scr, self.cfg, self.cfg_path = stdscr, cfg, cfg_path
        self.items = [Item(e) for e in cfg["projects"]]
        self.browse_dir = start_dir
        self.cursor = 0
        if self.rows()[0][0] == "sep":   # no projects yet: start on "Add a project…"
            self.move(1)
        self.dirty = False
        self.msg = ("Choose 'Add a project…' to pick a folder." if not self.items
                    else "%d projects on this node." % len(self.items))
        self.refresh_server_status()

    # ------------------------------------------------------------------ helpers
    def refresh_server_status(self):
        self.server = ns.local_health(self.cfg["port"])

    def attrs(self):
        G, R, Y, C = (curses.color_pair(i) for i in (1, 2, 3, 4))
        return G, R, Y, C, curses.A_BOLD, curses.A_DIM

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w - 1:
            try:
                self.scr.addstr(y, x, text[: w - 1 - x], attr)
            except curses.error:
                pass

    def prompt(self, label, default=""):
        """Ask on a normal terminal line (outside curses) so long paths and paste work."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            val = input("\n%s%s: " % (label, " [%s]" % default if default else "")).strip()
        except (EOFError, KeyboardInterrupt):
            val = ""
        finally:
            curses.reset_prog_mode()
            self.scr.clear()
        return val or default

    def choose(self, title, options, detail=None):
        """Full-screen menu. Returns the chosen index, or None on Esc/q/←."""
        cur = 0
        G, R, Y, C, B, D = self.attrs()
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(1, 2, title, B | C)
            if detail:
                self.put(2, 2, detail, D)
            for i, o in enumerate(options):
                self.put(4 + i, 2, ("> " if i == cur else "  ") + o, B if i == cur else 0)
            self.put(h - 2, 2, "↑↓ move · Enter choose · Esc back", D)
            self.scr.refresh()
            k = self.scr.getch()
            if k in (curses.KEY_UP, ord("k")):
                cur = (cur - 1) % len(options)
            elif k in (curses.KEY_DOWN, ord("j")):
                cur = (cur + 1) % len(options)
            elif k in ENTER_KEYS:
                return cur
            elif k in (27, ord("q"), curses.KEY_LEFT) + BACK_KEYS:
                return None

    def header(self):
        G, R, Y, C, B, D = self.attrs()
        self.put(0, 1, "netmon node setup", B | C)
        x = 20
        for txt, attr in (("node ", D), (self.cfg["node_name"], B), ("  port ", D),
                          (str(self.cfg["port"]), B), ("  server ", D)):
            self.put(0, x, txt, attr)
            x += len(txt)
        if self.server:
            self.put(0, x, "● running", G | B)
        else:
            self.put(0, x, "● not running", (R | B) if curses.has_colors() else curses.A_REVERSE)

    # ------------------------------------------------------------------ main screen
    def rows(self):
        return [("proj", i) for i in self.items] + [("sep", None)] + [("act", a) for a in ACTIONS]

    def draw_main(self):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        G, R, Y, C, B, D = self.attrs()
        self.header()
        self.put(1, 1, "config %s%s" % (tilde(self.cfg_path), "   · unsaved changes" if self.dirty else ""),
                 (Y if self.dirty else D))
        self.put(3, 1, "PROJECTS ON THIS NODE", C | B)

        rows = self.rows()
        top_y, view_h = 4, max(3, h - 8)
        top = max(0, min(self.cursor - view_h + 1, len(rows) - view_h)) if self.cursor >= view_h else 0
        nw = min(max([len(i.name) for i in self.items] + [8]), 28)
        if not self.items:
            self.put(top_y, 4, "(none yet)", D)
            top_y += 1
        for r, (kind, obj) in enumerate(rows[top:top + view_h]):
            idx, y = top + r, top_y + r
            cur = idx == self.cursor
            if kind == "sep":
                continue
            self.put(y, 1, ">" if cur else " ", B | C)
            if kind == "proj":
                ver = obj.version
                self.put(y, 3, "●", R if ver == "missing on disk" else G)
                self.put(y, 5, obj.name[:nw].ljust(nw), B if cur else 0)
                self.put(y, 7 + nw, ver[:30].ljust(30), R if ver == "missing on disk" else D)
                self.put(y, 39 + nw, tilde(obj.path), D)
            else:
                self.put(y, 3, obj[1], (B | C) if cur else C)
        self.put(h - 3, 1, self.msg, Y)
        self.put(h - 2, 1, MAIN_HELP, D)
        s.refresh()

    def move(self, delta):
        rows = self.rows()
        i = self.cursor
        for _ in range(len(rows)):
            i = (i + delta) % len(rows)
            if rows[i][0] != "sep":
                self.cursor = i
                return

    def loop(self):
        while True:
            self.draw_main()
            try:
                k = self.scr.getch()
            except KeyboardInterrupt:
                k = ord("q")
            kind, obj = self.rows()[self.cursor]
            if k in (curses.KEY_UP, ord("k")):
                self.move(-1)
            elif k in (curses.KEY_DOWN, ord("j")):
                self.move(1)
            elif k in ENTER_KEYS:
                if kind == "proj":
                    self.project_menu(obj)
                elif kind == "act":
                    if self.do_action(obj[0]):
                        return
            elif k in (ord("a"), ord("+")):
                self.add_project()
            elif k in (ord("d"), curses.KEY_DC) + BACK_KEYS and kind == "proj":
                self.remove(obj)
            elif k == ord("n"):
                self.cfg["node_name"] = self.prompt("Node name", self.cfg["node_name"])
                self.dirty = True
            elif k == ord("p"):
                v = self.prompt("Server port", str(self.cfg["port"]))
                if v.isdigit() and 0 < int(v) < 65536:
                    self.cfg["port"] = int(v)
                    self.dirty = True
                    self.refresh_server_status()
                else:
                    self.msg = "Invalid port: %s" % v
            elif k == ord("s"):
                self.do_action("save")
            elif k == ord("S"):
                self.do_action("start")
            elif k in (ord("q"), 27):
                if self.do_action("quit"):
                    return

    def do_action(self, act):
        """Returns True when the TUI should exit."""
        if act == "add":
            self.add_project()
        elif act == "save":
            self.save()
        elif act == "start":
            self.msg = "Starting server…"
            self.draw_main()
            self.start_server()
        elif act == "quit":
            if not self.dirty:
                return True
            c = self.choose("You have unsaved changes", ["Save and quit", "Quit without saving", "Cancel"])
            if c == 0:
                self.save()
                return True
            return c == 1
        return False

    def project_menu(self, item):
        c = self.choose(item.name, ["Back", "Remove from this node"],   # harmless option first
                        detail="%s   %s" % (tilde(item.path), item.version))
        if c == 1:
            self.remove(item)

    def remove(self, item):
        self.items.remove(item)
        self.cursor = min(self.cursor, len(self.items))  # stays in place / on the "Add" row
        if self.rows()[self.cursor][0] == "sep":
            self.move(1)
        self.dirty = True
        self.msg = "Removed %s — Save to apply." % item.name

    def add_project(self):
        path = self.browse()
        if not path:
            self.msg = "Nothing added."
            return
        existing = [i for i in self.items if i.path == path]
        if existing:
            self.cursor = self.items.index(existing[0])
            self.msg = "%s is already on this node." % existing[0].name
            return
        name = Path(path).name
        if any(i.name == name for i in self.items):          # keep names unique for the dashboard
            name = "%s-%s" % (Path(path).parent.name, name)
        entry = setup_project({"name": name, "path": path}, self)
        if entry is None:
            self.msg = "Cancelled."
            return
        self.items.append(Item(entry))
        self.cursor = len(self.items) - 1
        self.dirty = True
        self.msg = "Added %s (%s) — Save to apply." % (name, tilde(path))

    # ------------------------------------------------------------------ folder browser
    def browse(self):
        """Arrow-key folder browser. Returns the chosen folder path, or None."""
        path = Path(self.browse_dir).expanduser()
        if not path.is_dir():
            path = Path.home()
        cur, show_hidden, land_on, entered = 0, False, None, True
        markers = {}

        def marker(p):
            if p not in markers:
                markers[p] = project_marker(p)
            return markers[p]

        while True:
            try:
                dirs, err = list_dirs(path, show_hidden), None
            except OSError as e:
                dirs, err = [], "can't read this folder: %s" % (e.strerror or e)
            rows = [("use", path)]
            if path.parent != path:
                rows.append(("up", path.parent))
            rows += [("dir", path / d) for d in dirs]

            if land_on is not None:  # coming back up: land on the folder we just left
                cur = next((i for i, r in enumerate(rows) if r[1] == land_on), 0)
                land_on = None
            elif entered:            # just opened: project folders land on "Add", others on 1st subfolder
                first = 2 if path.parent != path else 1
                cur = 0 if marker(path) or len(rows) <= first else first
            entered = False
            cur = max(0, min(cur, len(rows) - 1))

            self.draw_browser(path, rows, cur, marker, err, show_hidden)
            k = self.scr.getch()
            kind, target = rows[cur]
            if k in (curses.KEY_UP, ord("k")):       # no wrap-around: holding ↓ never lands on "Add"
                cur = max(0, cur - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                cur = min(len(rows) - 1, cur + 1)
            elif k == curses.KEY_PPAGE:
                cur = max(0, cur - 10)
            elif k == curses.KEY_NPAGE:
                cur = min(len(rows) - 1, cur + 10)
            elif k == curses.KEY_HOME:
                cur = 0
            elif k == curses.KEY_END:
                cur = len(rows) - 1
            elif k in ENTER_KEYS and kind == "use":
                self.browse_dir = str(path.parent)    # next "Add" reopens next to this project
                return str(path)
            elif k in ENTER_KEYS + (curses.KEY_RIGHT, ord("l")) and kind in ("dir", "up"):
                if kind == "up":
                    land_on, path = path, target
                else:
                    path, entered = target, True
            elif k in (curses.KEY_LEFT, ord("h")) + BACK_KEYS and path.parent != path:
                land_on, path = path, path.parent
            elif k == ord(" ") and kind == "dir":          # shortcut: add highlighted folder
                self.browse_dir = str(path)
                return str(target)
            elif k == ord("."):
                show_hidden = not show_hidden
            elif k in (ord("/"), ord("g")):
                t = self.prompt("Go to folder", tilde(path))
                p = Path(t).expanduser()
                p = (p if p.is_absolute() else path / p).resolve()
                if p.is_dir():
                    path, entered = p, True
            elif k in (27, ord("q")):
                return None

    def draw_browser(self, path, rows, cur, marker, err, show_hidden):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        G, R, Y, C, B, D = self.attrs()
        self.put(0, 1, "Add a project", B | C)
        self.put(0, 15, "— choose its folder", D)
        self.put(1, 1, tilde(path), B)

        top_y, view_h = 3, max(3, h - 7)
        top = max(0, cur - view_h + 1)
        for r, (kind, p) in enumerate(rows[top:top + view_h]):
            idx, y = top + r, top_y + r
            sel = idx == cur
            self.put(y, 1, ">" if sel else " ", B | C)
            if kind == "use":
                m = marker(path)
                self.put(y, 3, "✓ Add this folder", (G | B) if sel else G)
                self.put(y, 22, "(%s)" % path.name if not m else "(%s · %s)" % (path.name, m), D)
            elif kind == "up":
                self.put(y, 3, "..", B if sel else 0)
                self.put(y, 7, "up to %s" % tilde(p), D)
            else:
                self.put(y, 3, p.name + "/", B if sel else 0)
                m = marker(p)
                if m:
                    self.put(y, 5 + len(p.name), "● " + m, G)
        if err:
            self.put(top_y + len(rows), 3, err, R)
        if len(rows) > view_h:
            self.put(top_y + view_h, 3, "… %d of %d" % (cur + 1, len(rows)), D)
        self.put(h - 3, 1, "↑↓ move · Enter/→ open · ←/Backspace up · Enter on ✓ to add the folder you're in", D)
        self.put(h - 2, 1, "space add highlighted · . %s hidden · / type a path · Esc cancel"
                 % ("hide" if show_hidden else "show"), D)
        s.refresh()

    # ------------------------------------------------------------------ save / server
    def save(self):
        self.cfg["projects"] = [i.to_entry() for i in self.items]
        self.cfg["browse_dir"] = self.browse_dir
        self.cfg.pop("scan_root", None)
        ns.save_config(self.cfg, self.cfg_path)
        self.dirty = False
        n = len(self.items)
        if self.server:
            self.msg = "Saved %d projects. The running server picks this up immediately." % n
        else:
            self.msg = "Saved %d projects. Choose 'Save & start server' to start it." % n

    def start_server(self):
        self.save()
        pid = ns.running_pid()
        if self.server:  # running on the configured port; config is reloaded per request
            self.msg = "Saved; server already running on port %d." % self.cfg["port"]
            return
        if pid:  # running, but on a different port -> restart
            subprocess.run([sys.executable, SERVER_SCRIPT, "stop"], stdout=subprocess.DEVNULL)
        r = subprocess.run([sys.executable, SERVER_SCRIPT, "start", "--config", str(self.cfg_path)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.refresh_server_status()
        self.msg = (r.stdout.strip().splitlines() or ["?"])[-1]


def run_tui(cfg, cfg_path, start_dir):
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")

    def _main(stdscr):
        _cursor(0)
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = 0
        if curses.has_colors():
            for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_CYAN), 1):
                curses.init_pair(i, c, bg)
        SetupTUI(stdscr, cfg, cfg_path, start_dir).loop()

    curses.wrapper(_main)
    cfg = ns.load_config(cfg_path)
    h = ns.local_health(cfg["port"])
    print("Config: %s  (%d projects)" % (cfg_path, len(cfg["projects"])))
    if h:
        print("Server: running on port %d as node '%s'" % (cfg["port"], h["node"]))
    else:
        print("Server: not running — start it with:  python3 %s start" % SERVER_SCRIPT)


# ----------------------------------------------------------------------------- CLI

def print_list(cfg, cfg_path):
    print("node '%s', port %d, config %s" % (cfg["node_name"], cfg["port"], cfg_path))
    if not cfg["projects"]:
        print("  (no projects selected)")
    for e in cfg["projects"]:
        st = ns.project_status(e)
        print("  %-24s %-28s %s" % (st["name"], st["version"] or "MISSING", st["path"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Choose which projects this node broadcasts (run on the node).")
    ap.add_argument("--config", type=Path, default=ns.DEFAULT_CONFIG, help="node config (default: %(default)s)")
    ap.add_argument("--start-dir", help="folder the browser opens in (default: last used, else your home)")
    ap.add_argument("--list", action="store_true", help="print the current config and exit")
    ap.add_argument("--add", nargs="+", metavar="PATH", help="add project folders (no TUI)")
    ap.add_argument("--remove", nargs="+", metavar="NAME_OR_PATH", help="remove projects (no TUI)")
    ap.add_argument("--name", help="set the node name (no TUI)")
    ap.add_argument("--port", type=int, help="set the server port (no TUI)")
    args = ap.parse_args(argv)
    cfg_path = args.config.expanduser()
    cfg = ns.load_config(cfg_path)

    if args.add or args.remove or args.name or args.port:
        if args.name:
            cfg["node_name"] = args.name
        if args.port:
            cfg["port"] = args.port
        for p in args.remove or []:
            rp = str(Path(p).expanduser().resolve())
            cfg["projects"] = [e for e in cfg["projects"] if e.get("name") != p and e["path"] != rp]
        for p in args.add or []:
            rp = str(Path(p).expanduser().resolve())
            if not Path(rp).is_dir():
                print("warning: %s is not a folder (added anyway)" % rp)
            if all(e["path"] != rp for e in cfg["projects"]):
                cfg["projects"].append({"name": Path(rp).name, "path": rp})
        ns.save_config(cfg, cfg_path)
        print_list(cfg, cfg_path)
        return 0
    if args.list:
        print_list(cfg, cfg_path)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("node_setup needs an interactive terminal (over ssh use: ssh -t host ...), "
                 "or use --list / --add / --remove.")
    run_tui(cfg, cfg_path, args.start_dir or cfg.get("browse_dir") or str(Path.home()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
