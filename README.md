# Network Configurine (netmon)

A tiny terminal dashboard for a handful of machines, plus a tiny status server
that runs on each of them. Pure Python standard library (3.7+): nothing to
`pip install`, on either side.

## Files

| file | where it runs | what it does |
|---|---|---|
| `monitor.py` | **your machine** | the dashboard (reads `network_config.json`) |
| `network_config.example.json` | your machine | template for `network_config.json` |
| `node_setup.py` | **each node** | terminal UI (with a folder browser) to pick which project folders the node broadcasts |
| `node_server.py` | **each node** | the mini web server (`/health`, `/projects`) |
| `deploy_node.sh` | your machine | copies the two node files to a node over ssh and opens the setup UI |

---

## 1. The dashboard (on local machine)

```bash
cp network_config.example.json network_config.json   # then edit hosts
python3 monitor.py                                   # live view, q quits, r refreshes
python3 monitor.py --once                            # print once and exit (exit 1 if any server is down)
```

Each node row has two dots:

* **P** (ping): green means it replied, red means no reply, grey means `ping` isn't available.
* **S** (server): green means `node_server.py` answered `/health`, red means it didn't (the reason is shown).

Each project row has its own dot. Green means the folder is present. Yellow means
it's a git repo with uncommitted changes. Red means the folder is missing on the
node, or it's listed in `expected_projects` but the node doesn't broadcast it.

The **version** is worked out on the node, from the best source available:
1. A git tag: `git describe --tags --dirty`, e.g. `v1.2.0` or `v1.2.0-3-gabc1234-dirty`.
2. A version declared in `package.json`, `pyproject.toml`, `Cargo.toml`, `setup.cfg`/`setup.py`
   or a `VERSION` file, plus the commit if there is one: `0.4.1 @d1f5351`.
3. Just the commit: `@d1f5351`.

Branch and last-commit date are shown alongside.

`network_config.json` fields: `nodes` is a list of `{name, host, port?, expected_projects?}`,
or just a bare host string. The optional keys are `default_port` (8765),
`refresh_seconds` (10) and `timeout_seconds` (2). The file is re-read automatically when you edit it.

---

## 2. Setting up a node (run on the node, usually over ssh)

**Easiest:** from your machine, `./deploy_node.sh olin@192.168.1.31`. It copies the
node scripts into `~/netmon` on the node and opens the setup UI there.

**Manually:**

```bash
scp node_server.py node_setup.py olin@192.168.1.31:~/netmon/
ssh -t olin@192.168.1.31 'python3 ~/netmon/node_setup.py'      # -t is needed for the UI
```

The setup UI works with just the arrow keys and Enter. The main screen lists the
projects on this node, followed by **+ Add a project…**, **Save**, **Save & start server** and **Quit**.

**Adding a project** opens a folder browser at your home directory, or wherever you last added from.
For example, to add `~/Projects/myproject`: move to `Projects/` and press Enter, then
`myproject/` and Enter, then Enter on **✓ Add this folder**.

| key | in the folder browser |
|---|---|
| ↑ ↓ | move |
| Enter or → | open the highlighted folder |
| ← or Backspace | go up a level |
| Enter on **✓ Add this folder** | add the folder you're in |
| space | add the highlighted folder without opening it |
| `.` | show/hide hidden folders |
| `/` | type a path to jump to |
| Esc | cancel |

Folders that look like projects (git repo, `package.json`, `pyproject.toml`, …) are marked
with a green ● and the reason. When you open one, the cursor starts on **✓ Add this folder**,
so it's Enter, Enter, Enter all the way down.

On the main screen, Enter on a project lets you remove it. There are also shortcuts:
`a` add, `d` remove, `n` node name, `p` port, `s` save, `S` save and start the server, `q` quit.
**Save & start server** starts it in the background, so it keeps running after you log out.

Selections are saved to `~/.netmon/node.json`. A running server re-reads that file on every
request, so changes show up on the dashboard without a restart.

Managing the server directly on the node:

```bash
python3 ~/netmon/node_server.py start | stop | status
python3 ~/netmon/node_server.py run          # foreground, logs every request
```

Scriptable, with no UI:

```bash
python3 node_setup.py --list
python3 node_setup.py --add ~/Projects/foo ~/Desktop/bar --remove oldproj --name pi --port 8765
```

### Notes (??)

* **Firewall:** the dashboard machine must be able to reach the node's port (default 8765),
  e.g. `sudo ufw allow 8765/tcp`.
* **Survive reboots:** `start` doesn't survive a reboot. For that, add a systemd user service
  (`~/.config/systemd/user/netmon.service`):
  ```ini
  [Service]
  ExecStart=/usr/bin/python3 %h/netmon/node_server.py run --quiet
  Restart=always
  [Install]
  WantedBy=default.target
  ```
  Then run `systemctl --user enable --now netmon` and `sudo loginctl enable-linger $USER`.
* **Security:** the server is read-only, but anyone on the network who can reach the port
  can see project names, paths and versions. Keep it on a trusted LAN or VPN, or bind it with `--host`.
* **Next step (per-project setup/actions):** `setup_project()` in `node_setup.py` runs right after
  a project is picked. Right now it just accepts the project, but that's where per-project
  questions go (e.g. "what command runs this?"). Answers are stored as extra keys on the project's
  entry in `~/.netmon/node.json`, which are preserved and available to `node_server.py`. Its hook
  point for new endpoints is marked in `Handler.do_GET`.
