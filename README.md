# Akuarium

A transparent desktop aquarium for Linux — fish, flora, starfish, crustaceans, bubbles, and light effects sitting under your windows.

## Requirements

- Linux with an X11 or XWayland session (best with KDE/KWin)
- Python 3.10+
- [PyQt6](https://pypi.org/project/PyQt6/)
- [python-xlib](https://pypi.org/project/python-xlib/) (keep-below / window hints)

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 akuarium.py
```

Pick a ground texture on startup, then use **F12** to open controls (shop, scales, effects).

### Desktop launcher (optional)

Edit `akuarium.desktop` so `Exec` and `Path` point at your install directory, then copy it into `~/.local/share/applications/`.

## Features

- Click-through transparent overlay that stays under other windows
- Shop for fish, krustaceans, starfish, and flora (drop PNGs into the asset folders)
- God rays, frost, blur, bubbles, and relics
- Optional **Cooler Boost** reaction on MSI laptops (`msi-ec`), with a fan-RPM fallback on other machines that expose hwmon fans

## Asset folders

| Folder | Contents |
|--------|----------|
| `Fish/` | Swimming fish sprites |
| `Krustaceans/` | Bottom crawlers |
| `Starfish/` | Starfish |
| `Flora/` | Plants |
| `Ground/` | Seabed textures (picked at launch) |
| `Reliks/` | Decorative relics |
| `Effects/` | Bubble sprite, etc. |
| `backgrounds/` | Optional tank backgrounds |

## License

Assets and code are provided as-is for personal use unless otherwise noted. Add a license file if you redistribute under specific terms.
