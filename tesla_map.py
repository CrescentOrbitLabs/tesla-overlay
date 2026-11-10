"""
Top-down map export for Tesla dashcam clips.

Uses the GPS track embedded in the clip and OpenStreetMap map tiles (the
dark style is the same OSM map recolored locally). Produces:
  * <name>_map.png      the whole route, start and finish marked
  * <name>_map.mp4      animated map, same length as the clip, car moving along

Tiles are cached on disk so repeat exports don't re-download them.
"""
import io
import math
import os
import subprocess
import sys
import urllib.request
from datetime import timedelta

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import tesla_overlay_core as core

USER_AGENT = "TeslaOverlay/1.0.4 (dashcam overlay app; +https://buymeacoffee.com/joshuah1998)"

# Tile usage follows the OpenStreetMap tile policy: identifying User-Agent,
# a few tiles per export, cached on disk, attribution shown on every output.
OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "\u00a9 OpenStreetMap contributors"
TILE = 256
MAX_ZOOM = 18
STYLES = ("light", "dark")

BLUE = (62, 132, 255)


def cache_dir():
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or home
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Caches")
    else:
        base = os.path.join(home, ".cache")
    d = os.path.join(base, "TeslaOverlay", "tiles")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------

def world_px(lat, lon, Z):
    """Web Mercator pixel coordinates in a world of 256px tiles at zoom Z."""
    n = 256 * (2 ** Z)
    lat = max(min(lat, 85.0511), -85.0511)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def haversine_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# Track
# --------------------------------------------------------------------------

def gps_track(telemetry):
    """
    Per-frame (lat, lon, heading_deg, speed_mps) with GPS updates interpolated
    so the car glides instead of jumping. Returns None if the clip has no GPS.
    """
    n = len(telemetry)
    fixes = []  # (frame, lat, lon)
    last = None
    for i, t in enumerate(telemetry):
        if t and (t["latitude_deg"] or t["longitude_deg"]):
            ll = (t["latitude_deg"], t["longitude_deg"])
            if ll != last:
                fixes.append((i, ll[0], ll[1]))
                last = ll
    if not fixes:
        return None
    out = []
    k = 0
    lastt = None
    for i in range(n):
        t = telemetry[i] or lastt
        lastt = t
        while k + 1 < len(fixes) and fixes[k + 1][0] <= i:
            k += 1
        f0 = fixes[k]
        if k + 1 < len(fixes) and i >= f0[0]:
            f1 = fixes[k + 1]
            r = (i - f0[0]) / max(1, f1[0] - f0[0])
            lat, lon = f0[1] + (f1[1] - f0[1]) * r, f0[2] + (f1[2] - f0[2]) * r
        else:
            lat, lon = f0[1], f0[2]
        out.append((lat, lon, (t["heading_deg"] if t else 0.0) % 360,
                    abs(t["vehicle_speed_mps"]) if t else 0.0))
    return out


# --------------------------------------------------------------------------
# Tiles
# --------------------------------------------------------------------------

def _darken(tile):
    """Turn a standard OSM tile into a dark-mode tile (invert, rotate hue back, dim)."""
    from PIL import ImageOps, ImageEnhance
    inv = ImageOps.invert(tile.convert("RGB"))
    h, s_, v = inv.convert("HSV").split()
    h = h.point(lambda x: (x + 128) % 256)
    out = Image.merge("HSV", (h, s_, v)).convert("RGB")
    out = ImageEnhance.Color(out).enhance(0.3)
    out = ImageEnhance.Brightness(out).enhance(0.85)
    out = ImageEnhance.Contrast(out).enhance(0.92)
    return out


# --------------------------------------------------------------------------
# Downloading tiles
# --------------------------------------------------------------------------
# One kept-alive HTTPS connection (as the OSM tile policy asks), retries with
# back-off, and, if Python's own connection keeps being cut off (some
# firewalls, antivirus HTTPS scanning and servers reject it), the system's
# built-in curl, which uses Windows' or macOS's own network stack.

import http.client
import shutil
import threading
import time
import urllib.parse

OSM_HOST = "tile.openstreetmap.org"
_net_lock = threading.Lock()
_conn = None
_use_curl = False


class MapDownloadError(RuntimeError):
    pass


FRIENDLY_NET_ERROR = (
    "Couldn't download the map from OpenStreetMap. Check that this computer is online, then try again. "
    "If it keeps happening, a firewall, antivirus web protection or network filter may be blocking "
    "tile.openstreetmap.org.")


def _tile_path(z, x, y):
    return os.path.join(cache_dir(), "osm", str(z), str(x), f"{y}.png")


def _new_connection():
    proxy = urllib.request.getproxies().get("https")
    if proxy:
        u = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        c = http.client.HTTPSConnection(u.hostname, u.port or 8080, timeout=20)
        c.set_tunnel(OSM_HOST, 443)
        return c
    return http.client.HTTPSConnection(OSM_HOST, 443, timeout=20)


def _get_python(z, x, y):
    """Download one tile over the shared connection. Returns bytes or raises."""
    global _conn
    last = None
    for attempt in range(4):
        try:
            if _conn is None:
                _conn = _new_connection()
            _conn.request("GET", f"/{z}/{x}/{y}.png", headers={"User-Agent": USER_AGENT, "Connection": "keep-alive"})
            r = _conn.getresponse()
            data = r.read()
            if r.status == 200 and data[:4] == b"\x89PNG":
                return data
            if r.status in (429, 500, 502, 503, 504):
                last = MapDownloadError(f"map server busy (HTTP {r.status})")
            else:
                raise MapDownloadError(f"map server refused the request (HTTP {r.status})")
        except MapDownloadError as e:
            if "refused" in str(e):
                raise
            last = e
        except (OSError, http.client.HTTPException) as e:
            last = e
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        time.sleep(0.5 * (2 ** attempt))
    raise last


def _get_curl(jobs):
    """Download tiles with the system curl in one go. jobs: [(z, x, y)]."""
    exe = shutil.which("curl")
    if not exe:
        raise MapDownloadError("curl not available")
    args = [exe, "-sSf", "--retry", "3", "--retry-delay", "1", "--connect-timeout", "15", "--max-time", "120",
            "-A", USER_AGENT, "--create-dirs"]
    if os.name == "nt":
        # Windows' curl checks certificate revocation, which fails behind many
        # antivirus HTTPS scanners and filtered networks
        args.append("--ssl-no-revoke")
    for z, x, y in jobs:
        args += ["-o", _tile_path(z, x, y) + ".part", OSM_URL.format(z=z, x=x, y=y)]
    p = subprocess.run(args, capture_output=True, text=True, errors="replace", creationflags=core._NOWINDOW)
    ok = 0
    for z, x, y in jobs:
        part = _tile_path(z, x, y) + ".part"
        try:
            with open(part, "rb") as fh:
                good = fh.read(4) == b"\x89PNG"
            if good:
                os.replace(part, _tile_path(z, x, y))
                ok += 1
            else:
                os.remove(part)
        except OSError:
            pass
    if ok < len(jobs):
        raise MapDownloadError((p.stderr or "curl failed").strip().splitlines()[-1] if p.stderr else "curl failed")


def ensure_tiles(jobs, progress=None):
    """Make sure every (z, x, y) tile is in the cache, downloading what's missing."""
    global _use_curl
    missing = [j for j in dict.fromkeys(jobs) if not os.path.exists(_tile_path(*j))]
    if not missing:
        return
    with _net_lock:
        done = 0
        pending = list(missing)
        if not _use_curl:
            while pending:
                z, x, y = pending[0]
                try:
                    data = _get_python(z, x, y)
                except MapDownloadError as e:
                    if "refused" in str(e):
                        raise MapDownloadError(FRIENDLY_NET_ERROR + f" ({e})")
                    _use_curl = True
                    break
                except Exception:
                    _use_curl = True  # Python's connection keeps getting cut off; let the system try
                    break
                path = _tile_path(z, x, y)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".part", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".part", path)
                pending.pop(0)
                done += 1
                if progress:
                    progress(done / len(missing))
        if pending:
            try:
                for i in range(0, len(pending), 40):
                    _get_curl(pending[i:i + 40])
                    if progress:
                        progress(min(1.0, (done + i + 40) / len(missing)))
            except Exception as e:
                raise MapDownloadError(FRIENDLY_NET_ERROR + f" ({str(e).strip()[:120]})")


def _fetch_tile(style, z, x, y):
    n = 2 ** z
    x %= n
    if not 0 <= y < n:
        return Image.new("RGB", (TILE, TILE), (40, 42, 48) if style == "dark" else (225, 225, 225))
    path = _tile_path(z, x, y)
    if not os.path.exists(path):
        ensure_tiles([(z, x, y)])
    tile = Image.open(path).convert("RGB")
    if tile.size != (TILE, TILE):
        tile = tile.resize((TILE, TILE), Image.LANCZOS)
    return _darken(tile) if style == "dark" else tile


def render_base(style, Z, left, top, w, h, progress=None):
    """Stitch the map for a pixel window at zoom Z."""
    img = Image.new("RGB", (w, h))
    tx0, ty0 = left // TILE, top // TILE
    tx1, ty1 = (left + w - 1) // TILE, (top + h - 1) // TILE
    n = 2 ** Z
    jobs = [(Z, tx % n, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1) if 0 <= ty < n]
    ensure_tiles(jobs, progress)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            img.paste(_fetch_tile(style, Z, tx, ty), (tx * TILE - left, ty * TILE - top))
    return img


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def _fit(track, size, margin=0.18, max_zoom=18):
    """Pick a zoom so the route fits, and the window (left, top) centering it."""
    lats = [p[0] for p in track]
    lons = [p[1] for p in track]
    for Z in range(max_zoom, 2, -1):
        xs, ys = zip(*(world_px(la, lo, Z) for la, lo in zip(lats, lons)))
        if max(xs) - min(xs) <= size * (1 - 2 * margin) and max(ys) - min(ys) <= size * (1 - 2 * margin):
            cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
            return Z, cx - size / 2, cy - size / 2
    Z = 3
    cx, cy = world_px(sum(lats) / len(lats), sum(lons) / len(lons), Z)
    return Z, cx - size / 2, cy - size / 2


def _glass_chip(img, xy, text, f, dark, anchor="lt"):
    d = ImageDraw.Draw(img)
    bb = d.textbbox((0, 0), text, font=f)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pad = int(f.size * 0.55)
    w, h = tw + pad * 2, th + pad * 2
    x, y = xy
    if anchor[0] == "r":
        x -= w
    if anchor[1] == "b":
        y -= h
    region = img.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(10))
    tint = Image.new("RGB", (w, h), (18, 20, 26) if dark else (255, 255, 255))
    region = Image.blend(region, tint, 0.55)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=255)
    img.paste(region, (x, y), mask)
    d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=h // 2,
                        outline=(70, 74, 86) if dark else (215, 218, 226), width=1)
    d.text((x + pad - bb[0], y + pad - bb[1]), text, font=f,
           fill=(240, 242, 248) if dark else (25, 28, 36))
    return w, h


def _route_line(d, pts, color, width, glow=None):
    if len(pts) < 2:
        return
    if glow:
        d.line(pts, fill=glow, width=int(width * 2.2), joint="curve")
    d.line(pts, fill=color, width=width, joint="curve")


def _car_marker(img, x, y, heading, s):
    """Draw onto an RGBA image (halo is composited so it stays translucent)."""
    r = 15 * s
    halo = Image.new("RGBA", (int(r * 4), int(r * 4)), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([r * 0.4, r * 0.4, r * 3.6, r * 3.6], fill=(62, 132, 255, 70))
    img.alpha_composite(halo, (int(x - r * 2), int(y - r * 2)))
    d = ImageDraw.Draw(img)
    d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255), outline=(62, 132, 255), width=max(2, int(3 * s)))
    a = math.radians(heading)
    pts = []
    for ang, rad in ((0, 0.75), (140, 0.62), (180, 0.25), (220, 0.62)):
        b = a + math.radians(ang)
        pts.append((x + math.sin(b) * r * rad, y - math.cos(b) * r * rad))
    d.polygon(pts, fill=BLUE)


def _end_marker(d, x, y, s, color):
    r = 10 * s
    d.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(255, 255, 255), width=max(2, int(3 * s)))


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def resolve_style(style, dark_hint=False):
    return style if style in STYLES else ("dark" if dark_hint else "light")


def _prepare(in_path, size, style, progress=None):
    telemetry = core.read_frame_telemetry(in_path)
    track = gps_track(telemetry)
    if not track:
        raise RuntimeError("This clip has no GPS location data, so a map can't be made.")
    Z, left, top = _fit(track, size, max_zoom=MAX_ZOOM)
    base = render_base(style, Z, int(left), int(top), size, size, progress)
    pix = [(world_px(la, lo, Z)[0] - int(left), world_px(la, lo, Z)[1] - int(top)) for la, lo, _, _ in track]
    return telemetry, track, base, pix


def _dark(style):
    return style == "dark"


def route_image(in_path, style="light", size=1600, progress=None):
    """Whole-route map as a PIL image."""
    _, track, base, pix = _prepare(in_path, size, style, progress)
    s = size / 1080
    img = base.convert("RGBA")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    _route_line(d, pix, BLUE + (255,), int(9 * s), glow=BLUE + (70,))
    _end_marker(d, *pix[0], s, (52, 199, 89))
    _end_marker(d, *pix[-1], s, (255, 69, 58))
    img = Image.alpha_composite(img, over).convert("RGB")

    dark = _dark(style)
    dist = sum(haversine_m(track[i][:2], track[i + 1][:2]) for i in range(len(track) - 1))
    when = core.start_time_from_name(in_path)
    title = (when.strftime("%b %d, %Y  %I:%M %p").replace(" 0", " ") + "   •   " if when else "") + \
        f"{dist / 1609.344:.2f} mi  ({dist / 1000:.2f} km)"
    _glass_chip(img, (int(24 * s), int(24 * s)), title, core.font(26 * s), dark)
    _glass_chip(img, (size - int(14 * s), size - int(14 * s)), ATTRIBUTION, core.font(16 * s), dark, "rb")
    return img


def export_map(in_path, out_dir=None, style="light", png=True, video=True, units="mph",
               progress=None, cancel=None):
    """Write <name>_map.png and/or <name>_map.mp4. Returns the files written."""
    root = os.path.splitext(os.path.basename(in_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(in_path))
    written = []
    report = progress or (lambda f: None)

    if png:
        img = route_image(in_path, style, 1600, progress=lambda f: report(0.1 * f))
        p = os.path.join(out_dir, root + "_map.png")
        img.save(p)
        written.append(p)
    if not video:
        report(1.0)
        return written

    size = 1080
    s = size / 1080
    telemetry, track, base, pix = _prepare(in_path, size, style, progress=lambda f: report(0.1 + 0.05 * f))
    _, _, dur = core.probe(in_path)
    n = len(track)
    fps = n / dur if dur > 0 else 36.0
    dark = _dark(style)
    when = core.start_time_from_name(in_path)

    # static layer: map + faded full route + start marker
    static = base.convert("RGBA")
    over = Image.new("RGBA", static.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    _route_line(od, pix, BLUE + (90,), int(8 * s))
    _end_marker(od, *pix[0], s, (52, 199, 89))
    static = Image.alpha_composite(static, over)
    attribution = ATTRIBUTION

    out = os.path.join(out_dir, root + "_map.mp4")
    enc = subprocess.Popen(
        [core.ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size}x{size}",
         "-framerate", f"{fps:.6f}", "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=core._NOWINDOW)
    cancelled = False
    try:
        trail = Image.new("RGBA", static.size, (0, 0, 0, 0))
        td = ImageDraw.Draw(trail)
        for i in range(n):
            if i > 0:  # extend the driven (solid) trail incrementally
                td.line([pix[i - 1], pix[i]], fill=BLUE + (255,), width=int(9 * s))
            frame = Image.alpha_composite(static, trail)
            lat, lon, hdg, spd = track[i]
            _car_marker(frame, pix[i][0], pix[i][1], hdg, s)
            frame = frame.convert("RGB")
            speed = spd * 2.236936 if units == "mph" else spd * 3.6
            label = f"{speed:.0f} {'mph' if units == 'mph' else 'km/h'}"
            if when:
                label = (when + timedelta(seconds=i / fps)).strftime("%I:%M:%S %p").lstrip("0") + "   •   " + label
            _glass_chip(frame, (int(24 * s), int(24 * s)), label, core.font(28 * s), dark)
            _glass_chip(frame, (size - int(14 * s), size - int(14 * s)), attribution, core.font(16 * s), dark, "rb")
            enc.stdin.write(frame.tobytes())
            if i % 10 == 0:
                report(0.15 + 0.85 * i / n)
            if cancel and cancel():
                cancelled = True
                break
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        err = enc.stderr.read().decode(errors="replace")
        enc.wait()
    if cancelled:
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError("Cancelled")
    if enc.returncode != 0:
        raise RuntimeError("Map video encoding failed:\n" + err.strip()[-600:])
    written.append(out)
    report(1.0)
    return written


# --------------------------------------------------------------------------
# Mini map embedded in the overlay video (top-right corner)
# --------------------------------------------------------------------------

class MiniMap:
    """
    Heading-up mini map, like a car's navigation screen: the map turns so the
    direction of travel is always up, the car stays put near the middle, the
    route ahead is faded and the driven trail is solid blue.
    """

    def __init__(self, telemetry, width, height, style="dark", position="bottom", progress=None):
        track = gps_track(telemetry)
        if not track:
            raise RuntimeError("no GPS location in this clip")
        self.track = track
        self.n = len(track)
        s = width / 1280.0
        self.s = s
        self.S = S = max(160, int(round(width * 0.25)) // 2 * 2)
        margin = int(18 * s)
        self.pos = (width - S - margin, margin if position == "bottom" else int(200 * s))
        self.dark = style == "dark"

        # zoom: closer in town, wider at highway speed
        fastest = max(p[3] for p in track)
        Z = 17 if fastest < 22 else 16
        world = [world_px(la, lo, Z) for la, lo, _, _ in track]
        R = self.R = int(S * 0.85) + 4  # crop radius that still covers the window after rotating
        left = int(min(x for x, _ in world)) - R - 8
        top = int(min(y for _, y in world)) - R - 8
        w = int(max(x for x, _ in world)) - left + R + 8
        h = int(max(y for _, y in world)) - top + R + 8
        base = render_base(style, Z, left, top, w, h, progress).convert("RGBA")
        self.pix = [(x - left, y - top) for x, y in world]

        # whole route, faded, drawn once
        lay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        _route_line(ImageDraw.Draw(lay), self.pix, BLUE + (110,), max(3, int(7 * s)))
        self.base = Image.alpha_composite(base, lay)

        # smoothed heading (unwrapped so 359 -> 1 doesn't spin the map)
        heads, prev, acc = [], None, 0.0
        for p in track:
            hd = p[2]
            if prev is None:
                acc = hd
            else:
                diff = (hd - prev + 540) % 360 - 180
                acc += diff
            prev = hd
            heads.append(acc)
        sm, out = heads[0], []
        for hd in heads:
            sm += (hd - sm) * 0.25
            out.append(sm)
        self.heading = out

        # frame pieces that never change
        rad = int(20 * s)
        self.mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(self.mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=rad, fill=255)
        pad = int(14 * s)
        sh = Image.new("RGBA", (S + pad * 2, S + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([pad, pad + int(4 * s), pad + S, pad + S + int(4 * s)], radius=rad,
                                             fill=(0, 0, 0, 120))
        self.shadow = sh.filter(ImageFilter.GaussianBlur(8 * s))
        self.shadow_pad = pad
        self.rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(self.rim).rounded_rectangle([0, 0, S - 1, S - 1], radius=rad,
                                                   outline=(255, 255, 255, 150), width=max(2, int(2 * s)))
        self.font_attr = core.font(max(9, 10.5 * s))
        self.font_n = core.font(max(9, 12 * s))
        self.trail_w = max(3, int(7 * s))

    def _static_layers(self):
        """Pieces that never change: attribution chip and compass disc."""
        S, s = self.S, self.s
        over = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        txt = ATTRIBUTION
        bb = d.textbbox((0, 0), txt, font=self.font_attr)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        bx, by = S - tw - int(10 * s), S - th - int(9 * s)
        d.rounded_rectangle([bx - int(5 * s), by - int(3 * s), S - int(5 * s), S - int(4 * s)], radius=int(5 * s),
                            fill=(15, 17, 24, 170) if self.dark else (255, 255, 255, 190))
        d.text((bx - bb[0], by - bb[1]), txt, font=self.font_attr,
               fill=(225, 228, 236) if self.dark else (40, 44, 56))
        ccx, ccy, cr = int(24 * s), int(24 * s), int(15 * s)
        d.ellipse([ccx - cr, ccy - cr, ccx + cr, ccy + cr],
                  fill=(20, 22, 30, 200) if self.dark else (255, 255, 255, 215))
        over.alpha_composite(self.rim)
        self.static_over = over
        self.compass = (ccx, ccy, cr)
        # map with the driven trail, drawn onto incrementally as the car moves
        self.work = self.base.copy()
        self.work_draw = ImageDraw.Draw(self.work)
        self.drawn_to = 0
        # shadow ring: only the part outside the map square ever shows
        sh = self.shadow.copy()
        pad = self.shadow_pad
        hole = Image.new("L", sh.size, 255)
        ImageDraw.Draw(hole).rounded_rectangle([pad, pad, pad + S - 1, pad + S - 1],
                                               radius=int(20 * s), fill=0)
        sh.putalpha(ImageChops.multiply(sh.getchannel("A"), hole))
        self.shadow_ring = sh

    def draw(self, img, idx):
        i = min(max(idx, 0), self.n - 1)
        S, s = self.S, self.s
        if not hasattr(self, "work"):
            self._static_layers()
        if i < self.drawn_to:  # went backwards (preview scrubbing): start the trail over
            self.work = self.base.copy()
            self.work_draw = ImageDraw.Draw(self.work)
            self.drawn_to = 0
        if i > self.drawn_to:
            seg = self.pix[max(self.drawn_to - 1, 0):i + 1]
            if len(seg) > 1:
                self.work_draw.line(seg, fill=BLUE + (255,), width=self.trail_w, joint="curve")
            self.drawn_to = i
        cx, cy = self.pix[i]
        hd = math.radians(self.heading[i])
        car_y = 0.64 * S
        # sample the turned map window directly: output (u, v) -> map (x, y)
        ca, sa = math.cos(hd), math.sin(hd)
        a, b = ca, -sa
        d_, e = sa, ca
        c = cx - a * (S / 2) - b * car_y
        f = cy - d_ * (S / 2) - e * car_y
        win = self.work.transform((S, S), Image.AFFINE, (a, b, c, d_, e, f), resample=Image.BILINEAR)

        _car_marker(win, S / 2, car_y, 0, s * 0.85)
        win.alpha_composite(self.static_over)
        d = ImageDraw.Draw(win)
        ccx, ccy, cr = self.compass
        nx, ny = ccx - math.sin(hd) * cr * 0.55, ccy - math.cos(hd) * cr * 0.55
        d.text((nx, ny), "N", font=self.font_n, fill=(255, 90, 90) if self.dark else (220, 40, 40), anchor="mm")

        px, py = self.pos
        pad = self.shadow_pad
        img.paste(self.shadow_ring, (px - pad, py - pad), self.shadow_ring)
        img.paste(win.convert("RGB"), (px, py), self.mask)
