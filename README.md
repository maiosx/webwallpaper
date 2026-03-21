# WebWallpaper 🌐

Use any website as your Windows desktop background — powered by a real Chromium engine.

---

## Requirements

- **Windows 10 / 11**
- **Python 3.10+**

Install dependencies (one-time):

```
pip install PyQt6 PyQt6-WebEngine
```

---

## Run

```
python WebWallpaper.py
```

A control panel appears and the website is placed behind your desktop icons.
The app minimises to the **system tray** — double-click the tray icon to reopen the panel.

---

## Features

| Feature | Details |
|---|---|
| Any website | Type or paste any URL |
| Favourites | Save & double-click to switch |
| Mute audio | On by default, toggleable |
| Auto-refresh | Reload the page on a timer (0 = off) |
| Zoom | Scale the page 25 – 400 % |
| Start with Windows | Adds / removes a registry run key |
| Tray icon | Quick reload, open panel, or quit |

---

## Tips

- **Best sites to use:** `earth.nullschool.net`, `windy.com`, any live dashboard, Fluid Simulation, or your own local HTML file via `file:///C:/path/to/file.html`
- **For full desktop embedding** (behind icons) run the script as Administrator once. After that you can run it normally.
- **Config** is saved at `%USERPROFILE%\.webwallpaper\config.json`

---

## Uninstall

1. Quit from the tray icon.
2. Uncheck "Start with Windows" in the Settings panel before quitting (or delete the registry key manually: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\WebWallpaper`).
3. Delete the script and `%USERPROFILE%\.webwallpaper\`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Wallpaper not showing behind icons | Run as Administrator |
| Black screen | Some sites block embedding; try a different URL |
| High CPU / RAM | WebEngine runs a real browser — close other heavy apps |
| Audio plays | Enable "Mute audio" in Settings and save |
