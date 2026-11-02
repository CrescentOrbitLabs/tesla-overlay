"""
Tesla Overlay: drop in Tesla dashcam clips, get copies with speed, gear,
steering, pedals, blinkers, Autopilot/FSD state and time burned in.

Run with no arguments for the window, or from a terminal:
    tesla_overlay clip1.mp4 clip2.mp4 [--kmh] [--top] [--gps] [--path] [--horizon=0.5]
                  [--flip-steering] [--map] [--map-corner] [--map-style=light|dark] [--no-map-video]
"""
import base64
import io
import json
import os
import sys
import threading

import tesla_overlay_core as core
import tesla_map

APP_NAME = "Tesla Overlay"
APP_VERSION = "1.0.0"
SUPPORT_URL = "https://buymeacoffee.com/joshuah1998"
SIGNATURE = "Copyright \u00a9 Joshua Hugley 2026  \u00b7  Made with \u2764\ufe0f in South Carolina"
SETTINGS = os.path.join(os.path.expanduser("~"), ".tesla_overlay.json")


def load_settings():
    try:
        with open(SETTINGS) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(d):
    try:
        merged = load_settings()
        merged.update(d)
        with open(SETTINGS, "w") as fh:
            json.dump(merged, fh)
    except Exception:
        pass


CAMERAS = (("left_repeater", "L Rear"), ("right_repeater", "R Rear"), ("left_pillar", "L Pillar"),
           ("right_pillar", "R Pillar"), ("front", "Front"), ("back", "Back"))


def camera_label(path):
    name = os.path.basename(path).lower()
    for key, label in CAMERAS:
        if key in name:
            return label
    return "Clip"


def system_is_dark():
    try:
        if sys.platform == "darwin":
            import subprocess
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
            return "dark" in r.stdout.lower()
        if os.name == "nt":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        pass
    return False


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


LICENSE_FILES = {"license": "LICENSE", "notices": "THIRD_PARTY_NOTICES.txt",
                 "gpl3": os.path.join("licenses", "GPL-3.0.txt"), "gpl2": os.path.join("licenses", "GPL-2.0.txt")}


def read_licenses():
    out = {}
    for key, rel in LICENSE_FILES.items():
        try:
            with open(resource(rel), encoding="utf-8") as fh:
                out[key] = fh.read()
        except OSError:
            out[key] = "This file is missing from the app bundle."
    return out


def overlay_opts(o):
    return dict(units=o.get("units", "mph"), position=o.get("position", "bottom"),
                show_gps=bool(o.get("show_gps")), driving_path=bool(o.get("driving_path")),
                horizon=float(o.get("horizon", core.DEFAULT_HORIZON)),
                flip_steering=bool(o.get("flip_steering")))


_minimap_cache = {}


def make_minimap(path, o):
    """Mini map for the video corner, or None if the option is off or the clip has no GPS."""
    if not o.get("inset_map"):
        return None
    style = o.get("resolved_map_style") or o.get("map_style", "dark")
    if style not in tesla_map.STYLES:
        style = "dark"
    position = o.get("position", "bottom")
    key = (path, style, position)
    if key not in _minimap_cache:
        telemetry = core.read_frame_telemetry(path)
        w, h, _ = core.probe(path)
        _minimap_cache.clear()
        _minimap_cache[key] = tesla_map.MiniMap(telemetry, w, h, style=style, position=position)
    return _minimap_cache[key]


def run_jobs(paths, o, out_dir, progress, cancel):
    """Overlay (and optionally map) every clip. progress(fraction, message). Returns (outputs, failed)."""
    outputs, failed = [], []
    do_map = bool(o.get("export_map"))
    map_style = o.get("resolved_map_style") or o.get("map_style", "light")
    if map_style not in tesla_map.STYLES:
        map_style = "light"
    share = 0.7 if do_map else 1.0
    for k, f in enumerate(paths):
        name = os.path.basename(f)
        base = k / len(paths)
        span = 1 / len(paths)
        out = core.default_output_path(f)
        if out_dir:
            out = os.path.join(out_dir, os.path.basename(out))
        hook = None
        if o.get("inset_map"):
            try:
                progress(base, f"Preparing map for {name}")
                hook = make_minimap(f, o).draw
            except Exception as e:
                failed.append(f"{name}: map corner skipped ({str(e).splitlines()[0]})")
        try:
            progress(base, f"Overlaying {name} ({k + 1} of {len(paths)})")
            outputs.append(core.convert(f, out, progress=lambda p: progress(base + span * share * p, None),
                                        cancel=cancel, frame_hook=hook, **overlay_opts(o)))
        except Exception as e:
            if cancel():
                break
            failed.append(f"{name}: {str(e).splitlines()[0]}")
            continue
        if do_map:
            try:
                progress(base + span * share, f"Building map for {name}")
                outputs += tesla_map.export_map(
                    f, out_dir or None, style=map_style, png=True, video=bool(o.get("map_video", True)),
                    units=o.get("units", "mph"),
                    progress=lambda p: progress(base + span * (share + (1 - share) * p), None), cancel=cancel)
            except Exception as e:
                if cancel():
                    break
                failed.append(f"{name} map: {str(e).splitlines()[0]}")
    return outputs, failed


def _data_url(img, fmt="JPEG", max_side=1280):
    img = img.copy()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(buf, "JPEG", quality=86)
    else:
        img.save(buf, fmt)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


class Api:
    """Methods callable from the web UI as window.pywebview.api.<name>(...)."""

    def __init__(self):
        self._window = None
        self._cancel = False
        self._map_cache = {}

    def _js(self, fn, *args):
        if self._window:
            self._window.evaluate_js(f"window.{fn} && window.{fn}(" + ",".join(json.dumps(a) for a in args) + ")")

    # settings
    def load_settings(self):
        return load_settings()

    def version(self):
        return APP_VERSION

    def save_settings(self, d):
        save_settings(d)
        return True

    # files
    def _describe(self, paths):
        out = []
        for p in paths:
            if p and p.lower().endswith(".mp4") and os.path.isfile(p):
                out.append({"path": p, "name": os.path.basename(p), "camera": camera_label(p),
                            "front": core.is_front_camera(p)})
        return out

    def pick_files(self):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        res = self._window.create_file_dialog(kind, allow_multiple=True,
                                              file_types=("MP4 video (*.mp4;*.MP4)", "All files (*.*)"))
        return self._describe(res or [])

    def pick_folder(self):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind)
        return (res[0] if isinstance(res, (list, tuple)) else res) if res else ""

    def dropped(self, paths):
        items = self._describe(paths)
        if items:
            self._js("addClips", items)

    # previews
    def preview(self, path, at, o):
        try:
            hook = None
            if o.get("inset_map"):
                try:
                    hook = make_minimap(path, o).draw
                except Exception:
                    hook = None  # no GPS in this clip; preview without the corner map
            img = core.preview(path, at=float(at), frame_hook=hook, **overlay_opts(o))
            return {"image": _data_url(img)}
        except Exception as e:
            return {"error": str(e)}

    def map_preview(self, path, o):
        style = o.get("resolved_map_style") or "light"
        key = (path, style)
        try:
            if key not in self._map_cache:
                self._map_cache[key] = _data_url(tesla_map.route_image(path, style, size=1000), "JPEG", 1000)
            return {"image": self._map_cache[key]}
        except Exception as e:
            return {"error": str(e).splitlines()[0] if str(e) else "Couldn't build the map."}

    # running
    def start(self, paths, o):
        self._cancel = False
        out_dir = o.get("out_dir") or ""

        def work():
            outputs, failed = run_jobs(paths, o, out_dir,
                                       lambda f, m: self._js("onProgress", min(f, 1.0), m),
                                       lambda: self._cancel)
            self._js("onProgress", 1.0, None)
            self._js("onDone", outputs, failed, self._cancel)
        threading.Thread(target=work, daemon=True).start()
        return True

    def cancel(self):
        self._cancel = True
        return True

    def open_url(self, url):
        import webbrowser
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            webbrowser.open(url)
        return True

    def get_licenses(self):
        return read_licenses()

    def reveal(self, path):
        import subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True


def webview2_installed():
    """True if Microsoft's Edge WebView2 runtime is installed (Windows only)."""
    import winreg
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    clients = "\\Microsoft\\EdgeUpdate\\Clients\\" + guid
    places = [(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node" + clients),
              (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE" + clients),
              (winreg.HKEY_CURRENT_USER, "Software" + clients)]
    for root, key in places:
        try:
            with winreg.OpenKey(root, key) as k:
                version = winreg.QueryValueEx(k, "pv")[0]
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            pass
    return False


def run_app():
    """Liquid glass window (system web view). Falls back to the classic window if unavailable."""
    if os.name == "nt" and not webview2_installed():
        return run_gui()
    try:
        import webview
    except Exception:
        return run_gui()
    with open(resource("ui.html"), encoding="utf-8") as fh:
        html = fh.read()
    api = Api()
    theme = load_settings().get("theme", "system")
    dark = theme == "dark" or (theme == "system" and system_is_dark())
    window = webview.create_window(APP_NAME, html=html, js_api=api, width=1240, height=840,
                                   min_size=(1120, 760), background_color="#07090e" if dark else "#e9edf5")
    api._window = window

    def bind_drop():
        try:
            from webview.dom import DOMEventHandler

            def on_drop(e):
                files = (e.get("dataTransfer") or {}).get("files") or []
                api.dropped([f.get("pywebviewFullPath") for f in files])
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            pass  # drag and drop is a convenience; the Add clips button always works

    window.events.loaded += bind_drop
    menu = None
    if sys.platform == "darwin":  # Mac menu bar; Windows uses the in-window License button
        try:
            from webview.menu import Menu, MenuAction
            menu = [Menu("Help", [
                MenuAction("License and Open Source Notices", lambda: window.evaluate_js("window.openLicense()")),
                MenuAction("Support me on Buy Me a Coffee", lambda: api.open_url(SUPPORT_URL)),
            ])]
        except Exception:
            menu = None
    try:
        if os.name == "nt":
            webview.start(gui="edgechromium")
        elif menu:
            webview.start(menu=menu)
        else:
            webview.start()
    except Exception:
        run_gui()


def run_cli(argv):
    files = [a for a in argv if not a.startswith("--")]
    map_style = next((a.split("=", 1)[1] for a in argv if a.startswith("--map-style=")), "light")
    opts = dict(units="kmh" if "--kmh" in argv else "mph",
                position="top" if "--top" in argv else "bottom",
                show_gps="--gps" in argv, driving_path="--path" in argv,
                horizon=next((float(a.split("=", 1)[1]) for a in argv if a.startswith("--horizon=")),
                             load_settings().get("horizon", core.DEFAULT_HORIZON)),
                flip_steering="--flip-steering" in argv or load_settings().get("flip_steering", False))
    opts.update(export_map="--map" in argv, map_style=map_style, map_video="--no-map-video" not in argv,
                inset_map="--map-corner" in argv, resolved_map_style=map_style)

    def progress(f, msg):
        if msg:
            print(f"\n{msg}", end="", flush=True)
        print(f"\r{f * 100:5.1f}%  ", end="", flush=True)
    outputs, failed = run_jobs(files, opts, "", progress, lambda: False)
    print()
    for o in outputs:
        print("  ->", o)
    for e in failed:
        print("  failed:", e)


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)

    files = []
    state = {"busy": False, "cancel": False}
    saved = load_settings()
    units = tk.StringVar(value=saved.get("units", "mph"))
    position = tk.StringVar(value=saved.get("position", "bottom"))
    show_gps = tk.BooleanVar(value=saved.get("show_gps", False))
    driving_path = tk.BooleanVar(value=saved.get("driving_path", True))
    horizon = tk.DoubleVar(value=saved.get("horizon", core.DEFAULT_HORIZON))
    flip = tk.BooleanVar(value=saved.get("flip_steering", False))

    export_map = tk.BooleanVar(value=saved.get("export_map", False))
    inset_map = tk.BooleanVar(value=saved.get("inset_map", False))

    def current_opts():
        return dict(units=units.get(), position=position.get(), show_gps=show_gps.get(),
                    driving_path=driving_path.get(), horizon=round(horizon.get(), 3),
                    flip_steering=flip.get(), export_map=export_map.get(), inset_map=inset_map.get(),
                    map_video=saved.get("map_video", True),
                    resolved_map_style=saved.get("map_style") if saved.get("map_style") in tesla_map.STYLES else "dark")

    def remember(*_):
        save_settings(current_opts())
    out_dir = tk.StringVar(value="")  # empty = next to each original
    status = tk.StringVar(value="Add one or more Tesla dashcam clips to begin.")

    frm = ttk.Frame(root, padding=14)
    frm.grid()

    import webbrowser

    def show_license():
        texts = read_licenses()
        win = tk.Toplevel(root)
        win.title(f"License \u00b7 {APP_NAME} {APP_VERSION}")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        for key, title in (("license", "Tesla Overlay"), ("notices", "Open source"),
                           ("gpl3", "GPL v3"), ("gpl2", "GPL v2")):
            page = ttk.Frame(nb)
            txt = tk.Text(page, width=86, height=30, wrap="word", font=("Courier", 10))
            sb = ttk.Scrollbar(page, command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            txt.insert("1.0", texts[key])
            txt.configure(state="disabled")
            txt.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            nb.add(page, text=title)

    menubar = tk.Menu(root)
    helpm = tk.Menu(menubar, tearoff=0)
    helpm.add_command(label="License and Open Source Notices...", command=show_license)
    helpm.add_command(label="Support me on Buy Me a Coffee", command=lambda: webbrowser.open(SUPPORT_URL))
    menubar.add_cascade(label="Help", menu=helpm)
    root.config(menu=menubar)
    ttk.Label(frm, text="Clips").grid(row=0, column=0, sticky="w")
    coffee = tk.Label(frm, text="\u2615  Support me on Buy Me a Coffee", fg="#b07800", cursor="hand2",
                      font=("TkDefaultFont", 10, "underline"))
    coffee.grid(row=0, column=1, sticky="n")
    coffee.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    lst = tk.Listbox(frm, width=62, height=7, activestyle="none")
    lst.grid(row=1, column=0, columnspan=3, sticky="we", pady=(2, 6))

    def add_files():
        picked = filedialog.askopenfilenames(
            title="Choose Tesla dashcam clips",
            filetypes=[("MP4 video", "*.mp4 *.MP4"), ("All files", "*.*")])
        for p in picked:
            if p not in files:
                files.append(p)
                lst.insert("end", os.path.basename(p))
        refresh()

    def remove_selected():
        for i in reversed(lst.curselection()):
            lst.delete(i)
            files.pop(i)
        refresh()

    def choose_out():
        d = filedialog.askdirectory(title="Save overlaid videos to")
        if d:
            out_dir.set(d)
            out_lbl.config(text=d)

    btns = ttk.Frame(frm)
    btns.grid(row=2, column=0, columnspan=3, sticky="w")
    add_btn = ttk.Button(btns, text="Add clips...", command=add_files)
    add_btn.pack(side="left")
    rem_btn = ttk.Button(btns, text="Remove", command=remove_selected)
    rem_btn.pack(side="left", padx=6)

    opts = ttk.Frame(frm)
    opts.grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 4))
    ttk.Label(opts, text="Speed:").pack(side="left")
    ttk.Radiobutton(opts, text="mph", value="mph", variable=units).pack(side="left")
    ttk.Radiobutton(opts, text="km/h", value="kmh", variable=units).pack(side="left", padx=(0, 16))
    ttk.Label(opts, text="Overlay:").pack(side="left")
    ttk.Radiobutton(opts, text="Bottom", value="bottom", variable=position).pack(side="left")
    ttk.Radiobutton(opts, text="Top", value="top", variable=position).pack(side="left", padx=(0, 16))
    ttk.Checkbutton(opts, text="Show GPS coordinates", variable=show_gps).pack(side="left")

    pathf = ttk.Frame(frm)
    pathf.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 4))
    ttk.Checkbutton(pathf, text="FSD-style driving path (front camera clips)",
                    variable=driving_path).pack(side="left")
    prev_btn = ttk.Button(pathf, text="Preview...")
    prev_btn.pack(side="left", padx=10)
    ttk.Checkbutton(pathf, text="Export top-down map", variable=export_map).pack(side="left", padx=(6, 0))
    ttk.Checkbutton(pathf, text="Map in video corner", variable=inset_map).pack(side="left", padx=(6, 0))

    outf = ttk.Frame(frm)
    outf.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 10))
    ttk.Button(outf, text="Output folder...", command=choose_out).pack(side="left")
    out_lbl = ttk.Label(outf, text="Same folder as each clip", foreground="gray")
    out_lbl.pack(side="left", padx=8)

    bar = ttk.Progressbar(frm, length=440, maximum=1.0)
    bar.grid(row=6, column=0, columnspan=2, sticky="we")
    go = ttk.Button(frm, text="Create videos")
    go.grid(row=6, column=2, sticky="e", padx=(8, 0))
    ttk.Label(frm, textvariable=status, wraplength=520).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(frm, text=SIGNATURE, foreground="gray").grid(row=8, column=0, columnspan=3, pady=(12, 0))

    def refresh():
        if not state["busy"]:
            go.config(state="normal" if files else "disabled", text="Create videos")
            prev_btn.config(state="normal" if files else "disabled")

    def open_preview():
        sel = lst.curselection()
        if sel:
            clip = files[sel[0]]
        else:
            clip = next((f for f in files if core.is_front_camera(f)), files[0])
        win = tk.Toplevel(root)
        win.title("Preview: " + os.path.basename(clip))
        win.resizable(False, False)
        try:
            vw, vh, _ = core.probe(clip)
        except Exception:
            vw, vh = 1280, 960
        k = min(720 / vw, 720 / vh, 1.0)
        blank = tk.PhotoImage(width=int(vw * k), height=int(vh * k))
        pic = ttk.Label(win, image=blank, anchor="center", compound="center", text="Loading...")
        pic.image = blank
        pic.grid(row=0, column=0, columnspan=2, padx=10, pady=10)
        at = tk.DoubleVar(value=0.3)
        ttk.Label(win, text="Moment in clip").grid(row=1, column=0, sticky="w", padx=10)
        ttk.Scale(win, from_=0.0, to=1.0, variable=at, length=520).grid(row=1, column=1, padx=10)
        ttk.Label(win, text="Path height\n(match the road)").grid(row=2, column=0, sticky="w", padx=10)
        ttk.Scale(win, from_=0.30, to=0.70, variable=horizon, length=520).grid(row=2, column=1, padx=10)
        ttk.Checkbutton(win, text="Flip steering direction (if the wheel and path turn the wrong way)",
                        variable=flip).grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 0))
        note = ttk.Label(win, foreground="gray", wraplength=640)
        note.grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 10))
        if driving_path.get() and not core.is_front_camera(clip):
            note.config(text="The driving path is only drawn on front camera clips.")
        else:
            note.config(text="Drag \"Path height\" until the blue line sits on the road. "
                             "The line shows where the car actually went next: solid while driving or "
                             "speeding up, faded while braking, white bar where it stopped.")
        job = {"pending": False, "again": False}

        def render():
            if job["pending"]:
                job["again"] = True
                return
            job["pending"] = True
            o = current_opts()
            a = at.get()

            def work():
                try:
                    hook = None
                    if o.get("inset_map"):
                        try:
                            hook = make_minimap(clip, o).draw
                        except Exception:
                            hook = None
                    img = core.preview(clip, at=a, frame_hook=hook, **overlay_opts(o))
                    img.thumbnail((720, 720))
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    data = base64.b64encode(buf.getvalue())
                    err = None
                except Exception as e:
                    data, err = None, str(e)
                root.after(0, show, data, err)
            threading.Thread(target=work, daemon=True).start()

        def show(data, err):
            job["pending"] = False
            if not win.winfo_exists():
                return
            if err:
                pic.config(text=err)
            else:
                photo = tk.PhotoImage(data=data)
                pic.config(image=photo, text="")
                pic.image = photo
            if job["again"]:
                job["again"] = False
                render()

        traces = [(v, v.trace_add("write", lambda *_: render()))
                  for v in (at, horizon, flip, driving_path, inset_map, units, position, show_gps)]

        def closed(e):
            if e.widget is win:
                for v, tid in traces:
                    v.trace_remove("write", tid)
                remember()
        win.bind("<Destroy>", closed)
        render()

    prev_btn.config(command=open_preview)

    def worker(jobs, opts, dest):
        def progress(f, msg):
            root.after(0, bar.config, {"value": f})
            if msg:
                root.after(0, status.set, msg + "...")
        done, failed = run_jobs(jobs, opts, dest, progress, lambda: state["cancel"])
        root.after(0, finished, done, failed)

    def finished(done, failed):
        state["busy"] = False
        for w in (add_btn, rem_btn):
            w.config(state="normal")
        bar.config(value=1.0 if done else 0)
        if state["cancel"]:
            status.set("Cancelled.")
        else:
            status.set(f"Done. {len(done)} file(s) created." + (f" {len(failed)} failed." if failed else ""))
        if failed and not state["cancel"]:
            messagebox.showwarning(APP_NAME, "\n\n".join(failed))
        refresh()

    def start():
        if state["busy"]:
            state["cancel"] = True
            go.config(state="disabled", text="Stopping...")
            return
        state.update(busy=True, cancel=False)
        for w in (add_btn, rem_btn):
            w.config(state="disabled")
        bar.config(value=0)
        go.config(text="Cancel")
        o = current_opts()
        remember()
        threading.Thread(target=worker, args=(list(files), o, out_dir.get()), daemon=True).start()

    go.config(command=start)
    refresh()
    # bring the window to the front on launch (macOS can open Tk apps behind others)
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]  # macOS Finder arg
    if args == ["--classic"]:
        run_gui()
    elif args:
        run_cli(args)
    else:
        run_app()
