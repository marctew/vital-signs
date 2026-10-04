# Vital Signs — design spec

Self-hosted digital signage. A web UI on a server controls which web pages and info are shown on each monitor attached to a Raspberry Pi.

This document records the decisions already made. Build to it. Where something is marked **Verify on device**, check it on the real hardware before committing to an approach, and record what you found in `docs/`.

## 1. Environment

| Part | Detail |
|---|---|
| Server | Ubuntu LXC on Proxmox, runs `vitalsigns-server` |
| Client | Raspberry Pi 5 on Raspberry Pi OS desktop, runs `vitalsigns-agent` |
| Displays | 2 × LG ultrawide in portrait, 1080 × 2560 each, side by side (left and right) |
| Network | LAN and Tailscale only. Server sits behind a Zoraxy reverse proxy. Not exposed to the internet |
| Repo | `github.com/marctew/vital-signs` |

Design for multiple Pis and any number of displays per Pi from the start, even though there is one Pi with two displays today.

## 2. Architecture

- The **server** is the single source of truth: displays, content, playlists, assignments, local pages, admin UI and API.
- The **agent** is a small Python service on the Pi. It launches and supervises one Chromium window per monitor and makes each one show what the server says it should.
- The agent drives Chromium over the **Chrome DevTools Protocol (CDP)**. Content is never iframed, so any URL works and logins persist.
- Communication is **REST only**. The agent polls the server roughly every 2 seconds. There is no WebSocket and no MQTT.
- Everything the agent displays is a URL. Local pages, uploads and external sites all look the same to it.

## 3. Repository layout

One monorepo, cloned in full on both devices. Each device runs only its own install script.

```
vital-signs/
  server/     Flask app, admin UI, API, database models
  agent/      Pi-side Python agent
  shared/     API contract and protocol version, used by both sides
  pages/      Version-controlled local pages (one folder per page)
  deploy/     install-server.sh, install-agent.sh, update.sh, systemd units
  docs/       Findings from on-device verification, setup notes
  CLAUDE.md
  DESIGN.md
```

- Runtime data (database, uploaded pages, config with secrets, Chromium profiles) lives outside the tracked tree or is gitignored. Ship example config files.
- Updating either device is `deploy/update.sh`: pull, install dependencies, restart the service.
- The agent reports its git SHA and protocol version on every poll. The UI shows when an agent is out of step with the server.

## 4. Server

**Stack:** Flask, SQLite, server-rendered templates with a little vanilla JS. No frontend build step. Runs as a plain systemd service, no Docker.

### Data model

- **Agent** — a Pi. Identified by hostname, auto-registers on first poll. Stores last seen time, git SHA, protocol version.
- **Display** — one monitor on an agent. Identified by agent plus output connector name (for example `HDMI-A-1`). Has a friendly name (`left`, `right`), last screenshot, and the resolution and orientation the agent reports for it. Nothing may assume 1080 × 2560 portrait: other Pis will have a single landscape screen or a different resolution. A display also has its own zoom multiplier, applied on top of each content item's zoom, so the same item can be reused across screens of different sizes.
- **Content item** — a URL plus rendering settings: zoom level, refresh interval, optional injected CSS. The URL can be external, a repo page or an uploaded page.
- **Playlist** — an ordered list of content items, each with a duration. A playlist of one item is a static screen.
- **Assignment** — which playlist a display is showing.
- **Override** — a temporary "show this URL on this display for N minutes", after which the display returns to its assignment.

### Agent API

The model is desired state plus a small command queue.

- **Poll** — the agent sends its identity, versions and per-display status. The server replies with the desired state for each display and a revision number, so an unchanged poll is cheap.
- **Commands** — one-shot actions (identify, reload, screenshot now) are queued per agent, delivered in the poll response and acknowledged by the agent.
- **Screenshots** — the agent uploads a thumbnail per display periodically and on content change.
- **Errors** — the agent reports page load failures so the UI can flag them.
- Agent requests carry a shared token from the agent's config file.

### Control API

REST endpoints for Home Assistant and n8n: list displays, assign a playlist, push an override, clear an override, trigger identify or reload. Same operations the UI uses. No auth in v1 beyond being LAN-only, but structure it so a token can be added.

### Admin UI

- Dashboard: every display with its live screenshot, current item, agent status and version drift.
- Manage content items, playlists and assignments.
- Push and clear overrides.
- Per-display actions: identify, reload.
- Local pages: list, upload, delete.

### Local pages

- Each page is a folder with an `index.html` and its assets, served at `/pages/<name>/`.
- Two sources, both scanned and offered in the UI as content automatically:
  - `pages/` in the repo, for pages built properly and deployed by `git pull`.
  - A data folder outside the repo, for pages uploaded through the UI as a zip or single HTML file.
- Uploads must be unpacked safely (no path traversal, sensible size limit).
- Provide a shared base stylesheet that is responsive: sized in viewport units with portrait and landscape layouts, so a page works on any display. 1080 × 2560 portrait is the primary target, not the only one.
- The agent appends the display's name, width, height and orientation as query parameters when loading a local page, so a page can adapt beyond what CSS can do.
- Pages can take query parameters so one page can be reused with different settings per display.
- **Data proxy:** pages fetch external data through the server, which avoids CORS and keeps API keys out of page source. Sources are named and configured server-side with their secrets. It must not be an open "fetch any URL" proxy.

## 5. Agent

**Stack:** Python, run as a systemd user service inside the Pi's graphical session, with desktop autologin enabled.

### Chromium

- One Chromium instance per display, each with its own persistent profile directory and its own CDP port bound to localhost.
- Launched in kiosk mode with crash-restore prompts, first-run screens and info bars suppressed.
- Persistent profiles keep site logins across restarts.

### Window placement

Each window must always land fullscreen on its own monitor. Left is always left.

- Raspberry Pi OS uses Wayland (labwc), where applications cannot position their own windows.
- **Preferred approach:** run Chromium under XWayland so `--window-position` is honoured. Place one window at the origin of each output, let kiosk mode fullscreen it there, then read the window bounds back over CDP and relaunch if wrong.
- **Fallback:** give each Chromium a distinct class and use labwc window rules to send it to a named output and fullscreen it.
- **Verify on device:** which approach actually works on the installed OS, labwc and Chromium versions. Document the result.

### Display mapping

- Displays are keyed on connector name, which is tied to the physical port and stable across reboots. Do not key on monitor model or serial, the two monitors are identical.
- Agent config maps connector to friendly name. **Verify on device:** the actual connector names.
- Agent config lists one or more outputs, each with its own rotation and position. A Pi with a single landscape screen is a config with one entry and no rotation. The install script must not assume two outputs or portrait.
- Output position and rotation are pinned in the compositor config from those settings, not handled in the browser. The install script sets this up.
- The agent reads each output's actual resolution and orientation at startup and reports them to the server.
- On startup the agent waits until every configured output is detected before launching any window.
- **Identify:** on command, each display shows its friendly name in large text for a few seconds.

### Playback

- Each playlist item is preloaded in its own tab. The agent rotates by switching the active tab, so there is no white flash or load wait.
- Each item reloads on its own refresh interval.
- Zoom and injected CSS are applied per item.
- An override takes over the display immediately and the playlist resumes when it expires.

### Resilience

- The last known desired state is cached on disk. The agent starts from the cache after a reboot and keeps running if the server is unreachable.
- If a page fails to load, skip to the next item and report it.
- A watchdog relaunches Chromium if it exits or stops responding over CDP.

### Remote access

Install wayvnc so sites can be logged in to once by remoting in. **Verify on device:** that it works alongside the chosen window placement approach.

## 6. Scope

**v1**

- Agent registration, polling and cached state
- Correct fullscreen placement on both monitors, with identify
- Content items, playlists with preloaded tab rotation, assignments
- Temporary overrides
- Per-item zoom, refresh interval and CSS injection
- Screenshot previews in the dashboard
- Local pages from the repo and from uploads, base stylesheet, data proxy
- Control API for Home Assistant and n8n
- Install and update scripts, systemd units

**v2, do not build yet but do not block**

- Split zones within one display (two or three stacked panes)
- Time-based schedules for assignments
- Scheduled screen power on and off
- Home Assistant integration beyond plain REST
- Update triggered from the UI
- Auth on the admin UI and control API

## 7. Suggested build order

1. On-device verification: connector names, rotation, window placement approach, wayvnc. Write up in `docs/`.
2. Agent prototype with hardcoded URLs: two kiosk windows on the right monitors, driven over CDP, surviving a reboot.
3. Shared API contract, then server skeleton: models, agent poll endpoint, registration.
4. Agent reconciles against server state, with caching and the watchdog.
5. Admin UI: content, playlists, assignments, dashboard with screenshots.
6. Playlist rotation with preloaded tabs, per-item settings, failure handling.
7. Overrides, identify and reload commands, control API.
8. Local pages: serving, upload, base stylesheet, data proxy, one example page.
9. Deploy scripts, systemd units, README for a fresh install on both devices.

## 8. Done means

- After a cold boot of the Pi with no keyboard attached, both monitors come up fullscreen on the correct sides showing their assigned playlists.
- Changing an assignment in the UI changes the screen within a few seconds.
- An override pushed over the API appears, then the playlist resumes on its own.
- Stopping the server leaves the screens running. Killing Chromium brings it back.
- A new page dropped into `pages/` and pulled on the server shows up as selectable content.
- A fresh LXC and a fresh Pi can each be set up from the repo with one install script.
