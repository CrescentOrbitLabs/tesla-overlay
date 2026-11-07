"""
Tesla Overlay core: reads the per-frame telemetry Tesla embeds in dashcam MP4s
(SEI user-data NAL units, protobuf schema from github.com/teslamotors/dashcam)
and burns a heads-up overlay into a new MP4 using a bundled ffmpeg.

No protobuf library needed: the schema is small, so it is decoded by hand.
"""
import math
import os
import re
import struct
import subprocess
import sys
from datetime import datetime, timedelta

from PIL import Image, ImageChops, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# MP4 / H.264 / protobuf parsing
# --------------------------------------------------------------------------

GEARS = {0: "P", 1: "D", 2: "R", 3: "N"}
AUTOPILOT = {0: None, 1: "FSD", 2: "AUTOSTEER", 3: "TACC"}

# field number -> (name, kind)
FIELDS = {
    1: ("version", "int"),
    2: ("gear_state", "int"),
    3: ("frame_seq_no", "int"),
    4: ("vehicle_speed_mps", "float"),
    5: ("accelerator_pedal_position", "float"),
    6: ("steering_wheel_angle", "float"),
    7: ("blinker_on_left", "bool"),
    8: ("blinker_on_right", "bool"),
    9: ("brake_applied", "bool"),
    10: ("autopilot_state", "int"),
    11: ("latitude_deg", "double"),
    12: ("longitude_deg", "double"),
    13: ("heading_deg", "double"),
    14: ("linear_acceleration_mps2_x", "double"),
    15: ("linear_acceleration_mps2_y", "double"),
    16: ("linear_acceleration_mps2_z", "double"),
}


def _varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def decode_sei_proto(buf):
    """Decode a SeiMetadata protobuf message into a dict (proto3 defaults filled)."""
    out = {name: (False if kind == "bool" else 0) for name, kind in FIELDS.values()}
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        fnum, wtype = key >> 3, key & 7
        if wtype == 0:
            val, i = _varint(buf, i)
        elif wtype == 1:
            val = struct.unpack_from("<d", buf, i)[0]
            i += 8
        elif wtype == 5:
            val = struct.unpack_from("<f", buf, i)[0]
            i += 4
        elif wtype == 2:
            ln, i = _varint(buf, i)
            i += ln
            continue
        else:
            raise ValueError("bad wire type")
        if fnum in FIELDS:
            name, kind = FIELDS[fnum]
            out[name] = bool(val) if kind == "bool" else val
    return out


def _strip_emulation(data):
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b == 0x03:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _sei_payload(nal):
    """Return protobuf bytes from a Tesla SEI NAL (magic 0x42... 0x69), else None."""
    i = 3
    while i < len(nal) and nal[i] == 0x42:
        i += 1
    if i <= 3 or i + 1 >= len(nal) or nal[i] != 0x69:
        return None
    return _strip_emulation(nal[i + 1:-1])


def _top_level_boxes(fp):
    fp.seek(0, 2)
    end = fp.tell()
    pos = 0
    while pos + 8 <= end:
        fp.seek(pos)
        size, kind = struct.unpack(">I4s", fp.read(8))
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", fp.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            break
        yield kind, pos + hdr, size - hdr
        pos += size


def read_frame_telemetry(path):
    """
    Return a list with one entry per video frame (decode order): a telemetry
    dict, or None if that frame had no SEI. The SEI that precedes a frame's
    slice belongs to that frame, matching Tesla's own viewer.
    """
    frames = []
    pending = None
    with open(path, "rb") as fp:
        for kind, start, size in _top_level_boxes(fp):
            if kind != b"mdat":
                continue
            fp.seek(start)
            data = fp.read(size)
            cur, n = 0, len(data)
            while cur + 4 <= n:
                nal_size = struct.unpack_from(">I", data, cur)[0]
                cur += 4
                if nal_size < 1 or cur + nal_size > n:
                    break
                nal_type = data[cur] & 0x1F
                if nal_type == 6 and nal_size > 2 and data[cur + 1] == 5:
                    payload = _sei_payload(data[cur:cur + nal_size])
                    if payload:
                        try:
                            pending = decode_sei_proto(payload)
                        except (ValueError, IndexError, struct.error):
                            pass
                elif nal_type in (1, 5):
                    # first_mb_in_slice == 0 (ue(v) leading bit '1') marks a new frame
                    if nal_size > 1 and data[cur + 1] & 0x80:
                        frames.append(pending)
                        pending = None
                cur += nal_size
    return frames


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------

def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


_NOWINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def source_bitrate(path):
    """Overall bitrate of the source in bits per second (0 if unknown)."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True,
                       errors="replace", creationflags=_NOWINDOW)
    m = re.search(r"bitrate: (\d+) kb/s", p.stderr)
    return int(m.group(1)) * 1000 if m else 0


def encoder_args(path, w, h):
    """x264 settings: good quality, with the file size kept close to the original's."""
    crf = "21" if w * h <= 2_000_000 else "23"
    args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf]
    src = source_bitrate(path)
    if src:
        cap = max(int(src * 1.4), 4_000_000)
        args += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
    return args


def probe(path):
    """Return (width, height, duration_seconds) by reading ffmpeg's banner."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, errors="replace",
                       creationflags=_NOWINDOW)
    err = p.stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not m:
        raise RuntimeError("Could not read video stream from " + os.path.basename(path))
    w, h = int(m.group(1)), int(m.group(2))
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    return w, h, dur


# --------------------------------------------------------------------------
# Overlay drawing
# --------------------------------------------------------------------------

_FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    # Windows
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "segoeuib.ttf"),
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_font_cache = {}


def font(size):
    size = max(8, int(size))
    if size in _font_cache:
        return _font_cache[size]
    f = None
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                pass
    if f is None:
        f = ImageFont.load_default(size=size)
    _font_cache[size] = f
    return f


WHITE = (255, 255, 255, 255)
DIM = (120, 120, 120, 255)
GREEN = (60, 220, 110, 255)
RED = (240, 60, 60, 255)
BLUE = (60, 140, 255, 255)
ACCENT_STRIP = (60, 140, 255, 150)
PANEL = (12, 12, 14, 200)


def _text_c(d, xy, text, f, fill):
    d.text(xy, text, font=f, fill=fill, anchor="mm")


def _arrow(d, cx, cy, s, left, fill):
    k = -1 if left else 1
    d.polygon([(cx + k * s, cy), (cx - k * s * 0.2, cy - s * 0.8),
               (cx - k * s * 0.2, cy - s * 0.3), (cx - k * s, cy - s * 0.3),
               (cx - k * s, cy + s * 0.3), (cx - k * s * 0.2, cy + s * 0.3),
               (cx - k * s * 0.2, cy + s * 0.8)], fill=fill)


# +1: positive steering angle = turning left. The app's "Flip steering
# direction" option sets this to -1 if the wheel and path turn the wrong way.
STEER_SIGN = 1


def _wheel(d, cx, cy, r, angle_deg, fill):
    w = max(2, int(r * 0.22))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fill, width=w)
    for spoke in (0, 180, 270):  # right, left and bottom spokes when straight
        a = math.radians(spoke + angle_deg * STEER_SIGN)
        d.line([(cx, cy), (cx + math.cos(a) * r, cy - math.sin(a) * r)], fill=fill, width=w)
    d.ellipse([cx - w, cy - w, cx + w, cy + w], fill=fill)


# --------------------------------------------------------------------------
# FSD-style driving path (front camera only)
# --------------------------------------------------------------------------
# Tesla does not record the planned path, so the line shows where the car
# actually went over the next few seconds. It follows the car's recorded
# heading (accurate and smooth); steering is only a fallback for clips without
# location data, because the steering sensor reads a few degrees off-center
# while driving straight. Like the in-car display: solid blue = keeps going or
# speeds up, faded blue = slowing, white bar = where the car comes to a stop.

PATH_BLUE = (62, 132, 255)
PATH_FADED = (150, 190, 255)
STEER_RATIO = 12.0      # steering wheel degrees per road wheel degree (fallback only)
WHEELBASE_M = 2.88
CAM_HEIGHT_M = 1.40     # front camera height above the road
PATH_WIDTH_M = 0.6
PATH_START_M = 4.5      # nearest point drawn
LOOKAHEAD_S = 5.0
MAX_DIST_M = 90.0
DEFAULT_HORIZON = 0.49  # horizon line as a fraction of frame height (measured on HW4 footage)


def smoothed_accel(telemetry, fps, half_window_s=0.5):
    """Longitudinal acceleration (m/s^2) per frame from speed, averaged over a window.
    Tesla reports speed in 0.05 mph steps, so single-frame differences are noise."""
    speed, last = [], None
    for t in telemetry:
        last = t or last
        speed.append(abs(last["vehicle_speed_mps"]) if last else 0.0)
    n = len(speed)
    w = max(1, int(round(fps * half_window_s)))
    return [(speed[min(i + w, n - 1)] - speed[max(i - w, 0)]) /
            max((min(i + w, n - 1) - max(i - w, 0)) / fps, 1e-6) for i in range(n)]


# Dashcam recording sizes by hardware. HW3 records every camera at 1280 x 960 (4:3,
# from its 1.2 MP sensors). HW4 records the front camera at its full 2896 x 1876
# (5.4 MP sensor) and the other cameras at half that, 1448 x 938.
HARDWARE_RESOLUTIONS = {
    "HW3": {"front": (1280, 960), "other": (1280, 960)},
    "HW4": {"front": (2896, 1876), "other": (1448, 938)},
}
HARDWARE_CHOICES = ("auto", "hw3", "hw4")
# Front camera horizontal field of view in the recorded frame. HW4 measured from
# real footage; HW3 records its 50 degree main camera.
HARDWARE_HFOV = {"HW3": 50.0, "HW4": 41.2}


def detect_hardware(width, height):
    """Best guess of the car's hardware from the clip's frame size, or None if it
    doesn't match a known size. Falls back to the aspect ratio so clips that were
    resized (for example by the Tesla app or another editor) are still recognized."""
    for hw, sizes in HARDWARE_RESOLUTIONS.items():
        if (width, height) in sizes.values():
            return hw
    if not width or not height:
        return None
    ratio = width / height
    if abs(ratio - 4 / 3) < 0.03:
        return "HW3"
    if abs(ratio - 2896 / 1876) < 0.03:
        return "HW4"
    return None


def resolve_hardware(choice, width, height):
    """The hardware to use: the user's choice ("hw3" / "hw4"), or the detected one for "auto".
    Unrecognized sizes fall back to HW4 when the frame is large, otherwise HW3."""
    choice = (choice or "auto").lower()
    if choice in ("hw3", "hw4"):
        return choice.upper()
    return detect_hardware(width, height) or ("HW4" if width >= 2000 else "HW3")


def camera_hfov(hardware):
    return HARDWARE_HFOV.get(hardware, 50.0)


# ---------------------------------------------------------------------------
# Advanced details the user can add to the video. The clips don't record these,
# so they are chosen by hand and drawn as a second strip above the panel.
SPEED_PROFILES = ("Sloth", "Chill", "Standard", "Hurry", "Mad Max")
CAR_MODELS = (
    ("Model S", ("Model S", "Model S Plaid")),
    ("Model 3", ("Model 3", "Model 3 Performance")),
    ("Model X", ("Model X", "Model X Plaid")),
    ("Model Y", ("Model Y", "Model Y Performance", "Model Y L")),
    ("Cybertruck", ("Cybertruck", "Cybertruck Cyberbeast")),
    ("Other", ("Cybercab", "Semi", "Roadster")),
)
FSD_VERSIONS = (
    ("FSD v14", ("14.3", "14.2", "14.1", "14.3 Lite", "14.1 Lite", "14 Lite")),
    ("FSD v13", ("13.2",)),
    ("FSD v12", ("12.6", "12.5", "12.4", "12.3", "12.2", "12.1")),
    ("Earlier", ("11.4", "11.3", "10.69", "10", "9")),
)
_ALL_MODELS = {m for _, group in CAR_MODELS for m in group}
_FSD_RE = re.compile(r"^\s*(?:fsd\s*)?v?\s*(\d{1,2}(?:\.\d{1,3}){0,3})\s*(lite)?\s*$", re.I)


def fsd_label(version):
    """'14.3.13' / 'v14.3 lite' / 'FSD 14' -> 'FSD v14.3.13' etc., or None if it isn't a version."""
    m = _FSD_RE.match(str(version or ""))
    if not m:
        return None
    return f"FSD v{m.group(1)}" + (" Lite" if m.group(2) else "")


def advanced_labels(car_model="", fsd_version="", speed_profile=""):
    """The text pieces for the advanced strip, skipping anything not set or not recognized."""
    parts = []
    if car_model in _ALL_MODELS:
        parts.append(car_model)
    label = fsd_label(fsd_version) if fsd_version else None
    if label:
        parts.append(label)
    if speed_profile in SPEED_PROFILES:
        parts.append(f"{speed_profile} profile")
    return parts


def is_front_camera(path):
    name = os.path.basename(path).lower()
    return "front" in name or not any(k in name for k in ("back", "rear", "repeater", "pillar", "left", "right"))


class DrivingPath:
    def __init__(self, telemetry, width, height, fps, horizon=DEFAULT_HORIZON, hardware=None):
        self.W, self.H, self.fps = width, height, fps
        self.horizon = horizon
        hardware = hardware or resolve_hardware("auto", width, height)
        self.f = (width / 2) / math.tan(math.radians(camera_hfov(hardware) / 2))
        # carry last known values forward through frames without data
        speed, steer, brake, heading, has_heading = [], [], [], [], []
        last = None
        acc = None
        for t in telemetry:
            last = t or last
            speed.append(abs(last["vehicle_speed_mps"]) if last else 0.0)
            steer.append(last["steering_wheel_angle"] if last else 0.0)
            brake.append(bool(last["brake_applied"]) if last else False)
            ok = bool(last and (last["latitude_deg"] or last["longitude_deg"]))
            has_heading.append(ok)
            if ok:
                h = last["heading_deg"]
                acc = h if acc is None else acc + ((h - acc + 540) % 360 - 180)  # unwrap
            heading.append(acc if acc is not None else 0.0)
        self.speed, self.steer, self.brake = speed, steer, brake
        self.heading, self.has_heading = heading, has_heading
        self._fade = None
        # acceleration over a one-second window: Tesla reports speed in 0.05 mph
        # steps, so frame-to-frame differences are mostly rounding noise
        self.accel = smoothed_accel(telemetry, fps, 0.5)

    def _trajectory(self, idx):
        """Points (x_right_m, z_fwd_m, kind) along the next few seconds; kind: go/slow."""
        n = len(self.speed)
        if n == 0 or self.speed[min(idx, n - 1)] < 0.4:
            return [], False
        idx = min(idx, n - 1)
        dt = 1.0 / self.fps
        use_heading = self.has_heading[idx]
        h0 = self.heading[idx]
        x = z = psi = dist = 0.0
        pts = [(0.0, 0.0, "go")]
        steps = int(LOOKAHEAD_S * self.fps)
        stopped = False
        for k in range(steps):
            j = min(idx + k, n - 1)
            v = self.speed[j]
            accel = self.accel[j]
            if use_heading and self.has_heading[j]:
                yaw = math.radians(self.heading[j] - h0)          # + = turned right (compass)
            else:
                delta = math.radians(self.steer[j] / STEER_RATIO) * STEER_SIGN
                psi += v * math.tan(delta) / WHEELBASE_M * dt     # + = turning left
                yaw = -psi
            ds = v * dt
            x += math.sin(yaw) * ds
            z += math.cos(yaw) * ds
            dist += ds
            kind = "slow" if (accel < -0.6 or self.brake[j]) else "go"
            pts.append((x, z, kind))
            if v < 0.3 and k > 0:
                stopped = True
                break
            if dist > MAX_DIST_M:
                break
        return pts, stopped

    def _project(self, x, z):
        return (self.W / 2 + self.f * x / z, self.H * self.horizon + self.f * CAM_HEIGHT_M / z)

    def draw(self, frame_img, idx):
        pts, stopped = self._trajectory(idx)
        pts = [p for p in pts if p[1] >= PATH_START_M]
        if len(pts) < 2:
            return
        top = int(self.H * self.horizon)
        h = self.H - top
        sc = 0.5  # draw at half size, soften, scale up: cheap anti-aliasing
        lw, lh = int(self.W * sc), int(h * sc)
        color = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        d = ImageDraw.Draw(color)

        def edge(base, a, b, side):
            # point offset sideways from base, perpendicular to direction a->b
            dx, dz = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(dx, dz) or 1.0
            ox, oz = dz / ln * PATH_WIDTH_M / 2 * side, -dx / ln * PATH_WIDTH_M / 2 * side
            u, v = self._project(base[0] + ox, max(base[1] + oz, 0.5))
            return (u * sc, (v - top) * sc)

        # one polygon per run of the same kind, so the line has no seams
        runs = []
        for i in range(len(pts) - 1):
            kind = pts[i + 1][2]
            if runs and runs[-1][0] == kind:
                runs[-1][2] = i + 1
            else:
                runs.append([kind, i, i + 1])
        for kind, a, b in runs:
            left, right = [], []
            for i in range(a, b + 1):
                p = pts[i]
                pa, pb = (p, pts[i + 1]) if i < len(pts) - 1 else (pts[i - 1], p)
                left.append(edge(p, pa, pb, -1))
                right.append(edge(p, pa, pb, 1))
            fill = PATH_BLUE + (215,) if kind == "go" else PATH_FADED + (120,)
            d.polygon(left + right[::-1], fill=fill)
        if stopped:
            # stop line painted on the road, across the path where the car comes to rest
            p, q = pts[-2], pts[-1]
            dx, dz = q[0] - p[0], q[1] - p[1]
            ln = math.hypot(dx, dz) or 1.0
            fx, fz = dx / ln, dz / ln          # forward
            sx, sz = fz, -fx                   # sideways
            corners = []
            for a, b in ((-1.1, -0.18), (1.1, -0.18), (1.1, 0.18), (-1.1, 0.18)):
                u, v = self._project(q[0] + sx * a + fx * b, max(q[1] + sz * a + fz * b, 0.5))
                corners.append((u * sc, (v - top) * sc))
            d.polygon(corners, fill=(255, 255, 255, 235))

        # work only on the area the line covers (it is narrow; the frame is huge)
        bbox = color.getbbox()
        if not bbox:
            return
        x0, y0 = max(bbox[0] - 2, 0), max(bbox[1] - 2, 0)
        x1, y1 = min(bbox[2] + 2, lw), min(bbox[3] + 2, lh)
        color = color.crop((x0, y0, x1, y1))
        # fade toward the horizon, applied once to the whole line
        if self._fade is None or self._fade.size != (1, lh):
            self._fade = Image.linear_gradient("L").resize((1, lh)).point(
                lambda v: int(255 * min(1.0, 0.45 + 0.55 * (v / 255) ** 0.6)))
        fade = self._fade.crop((0, y0, 1, y1)).resize(color.size)
        r, g, b_, a_ = color.split()
        a_ = ImageChops.multiply(a_, fade)
        from PIL import ImageFilter
        X0, Y0 = int(x0 / sc), int(y0 / sc)
        X1, Y1 = min(int(x1 / sc), self.W), min(int(y1 / sc), h)
        layer = Image.merge("RGBA", (r, g, b_, a_)).filter(ImageFilter.GaussianBlur(0.8)).resize(
            (X1 - X0, Y1 - Y0), Image.BILINEAR)
        box = (X0, top + Y0, X1, top + Y1)
        region = frame_img.crop(box).convert("RGBA")
        region.alpha_composite(layer)
        frame_img.paste(region.convert("RGB"), box[:2])


class OverlayRenderer:
    def __init__(self, width, height, units="mph", position="bottom",
                 show_gps=False, start_time=None, fps=36.0, telemetry=None, hardware_label=None,
                 details=None):
        self.W, self.H = width, height
        # what the car is doing, for the bars while FSD / Autosteer / TACC is driving
        self.accel = smoothed_accel(telemetry, fps, 0.3) if telemetry else None
        self.units = units
        self.position = position
        self.show_gps = show_gps
        self.hardware_label = hardware_label
        self.details = "   |   ".join(details) if details else ""
        self._details_img = None
        self.start_time = start_time
        self.fps = fps
        s = width / 1280.0
        self.s = s
        self.pw = int(min(width - 20 * s, 760 * s))
        self.ph = int(118 * s)
        self.px = (width - self.pw) // 2
        self.py = int(height - self.ph - 18 * s) if position == "bottom" else int(18 * s)

    def render(self, frame_img, t, idx):
        s = self.s
        panel = Image.new("RGBA", (self.pw, self.ph), (0, 0, 0, 0))
        d = ImageDraw.Draw(panel)
        d.rounded_rectangle([0, 0, self.pw - 1, self.ph - 1], radius=int(22 * s), fill=PANEL)

        if t is None:
            _text_c(d, (self.pw / 2, self.ph / 2), "NO DATA", font(28 * s), DIM)
        else:
            self._draw_data(d, t, idx)
        frame_img.paste(panel, (self.px, self.py), panel)

        # strips stacked away from the panel: the time / heading line, then the advanced details
        gap = int(8 * s)
        edge = self.py if self.position == "bottom" else self.py + self.ph
        info = self._info_line(t, idx)
        strips = [self._strip(info, WHITE)] if info else []
        if self.details:
            if self._details_img is None:
                self._details_img = self._strip(self.details, WHITE, tint=ACCENT_STRIP)
            strips.append(self._details_img)
        for box in strips:
            if self.position == "bottom":
                edge -= gap + box.height
                frame_img.paste(box, ((self.W - box.width) // 2, edge), box)
            else:
                edge += gap
                frame_img.paste(box, ((self.W - box.width) // 2, edge), box)
                edge += box.height

    def _strip(self, text, color, tint=None):
        """A small rounded label like the time line."""
        s = self.s
        f = font(17 * s)
        bbox = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=f)
        tw = bbox[2] - bbox[0]
        th = font(17 * s).getbbox("Hg0")[3] - font(17 * s).getbbox("Hg0")[1]
        pad = int(8 * s)
        box = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        bd = ImageDraw.Draw(box)
        bd.rounded_rectangle([0, 0, box.width - 1, box.height - 1], radius=int(8 * s), fill=PANEL)
        if tint:
            bd.rounded_rectangle([0, 0, box.width - 1, box.height - 1], radius=int(8 * s),
                                 outline=tint, width=max(1, int(1.5 * s)))
        bd.text((pad - bbox[0], pad - font(17 * s).getbbox("Hg0")[1]), text, font=f, fill=color)
        return box

    def _info_line(self, t, idx):
        parts = []
        if self.start_time:
            parts.append((self.start_time + timedelta(seconds=idx / self.fps)).strftime("%Y-%m-%d  %H:%M:%S"))
        if t and self.show_gps and (t["latitude_deg"] or t["longitude_deg"]):
            parts.append(f"{t['latitude_deg']:.5f}, {t['longitude_deg']:.5f}")
        if t and (t["latitude_deg"] or t["longitude_deg"]):
            h = t["heading_deg"] % 360
            card = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((h + 22.5) // 45) % 8]
            parts.append(f"{card} {h:.0f}\u00b0")
        if self.hardware_label:
            parts.append(self.hardware_label)
        return "   |   ".join(parts)

    def _draw_data(self, d, t, idx):
        s, pw, ph = self.s, self.pw, self.ph
        cy = ph / 2
        blink_on = (idx // max(1, int(self.fps / 3))) % 2 == 0  # ~1.5 Hz flash

        # Blinkers at the outer edges
        _arrow(d, 40 * s, cy, 22 * s, True, GREEN if t["blinker_on_left"] and blink_on else DIM)
        _arrow(d, pw - 40 * s, cy, 22 * s, False, GREEN if t["blinker_on_right"] and blink_on else DIM)

        # Speed (center)
        mps = abs(t["vehicle_speed_mps"])
        spd = mps * 2.236936 if self.units == "mph" else mps * 3.6
        _text_c(d, (pw / 2, cy - 10 * s), f"{spd:.0f}", font(64 * s), WHITE)
        _text_c(d, (pw / 2, cy + 38 * s), self.units.upper(), font(16 * s), DIM)

        # Gear selector (left of speed)
        gear = GEARS.get(t["gear_state"], "?")
        gx = pw / 2 - 175 * s
        for i, g in enumerate("PRND"):
            _text_c(d, (gx + (i - 1.5) * 28 * s, cy), g, font(26 * s), WHITE if g == gear else DIM)

        # Autopilot badge (under gears)
        ap = AUTOPILOT.get(t["autopilot_state"])
        if ap:
            f = font(15 * s)
            bw = d.textlength(ap, font=f) + 18 * s
            d.rounded_rectangle([gx - bw / 2, cy + 22 * s, gx + bw / 2, cy + 46 * s],
                                radius=int(12 * s), fill=BLUE)
            _text_c(d, (gx, cy + 34 * s), ap, f, WHITE)

        # Steering wheel + angle (right of speed)
        wx = pw / 2 + 120 * s
        ang = t["steering_wheel_angle"]
        _wheel(d, wx, cy - 6 * s, 24 * s, ang, BLUE if ap in ("FSD", "AUTOSTEER") else WHITE)
        _text_c(d, (wx, cy + 36 * s), f"{ang:+.0f}\u00b0", font(14 * s), DIM)

        # Two bars. You driving: accelerator and brake pedals. FSD / Autosteer /
        # TACC driving (pedals untouched): the car's own speeding up and slowing
        # down, outlined in blue, labelled ACC / DEC.
        assisted = ap is not None and self.accel is not None
        brake = bool(t["brake_applied"])
        if assisted:
            a = self.accel[min(idx, len(self.accel) - 1)]
            acc = min(a / 2.5, 1.0) if a > 0.1 else 0.0          # full bar at about 0.25 g
            dec = 1.0 if brake else (min(-a / 3.0, 1.0) if a < -0.1 else 0.0)
            frame_col, labels = BLUE, ("ACC", "DEC")
        else:
            acc = t["accelerator_pedal_position"]
            acc = acc / 100.0 if acc > 1.5 else acc
            acc = min(max(acc, 0.0), 1.0)
            dec = 1.0 if brake else 0.0
            frame_col, labels = DIM, ("ACC", "BRK")
        bx, bt, bb, bwid = pw / 2 + 185 * s, cy - 30 * s, cy + 22 * s, 14 * s
        lw = max(1, int(2 * s))
        d.rounded_rectangle([bx, bt, bx + bwid, bb], radius=int(4 * s), outline=frame_col, width=lw)
        if acc > 0.01:
            d.rounded_rectangle([bx, bb - (bb - bt) * acc, bx + bwid, bb], radius=int(4 * s), fill=GREEN)
        _text_c(d, (bx + bwid / 2, cy + 36 * s), labels[0], font(11 * s), DIM)
        rx = bx + 30 * s
        d.rounded_rectangle([rx, bt, rx + bwid, bb], radius=int(4 * s),
                            outline=RED if dec >= 0.999 else frame_col, width=lw)
        if dec > 0.01:
            d.rounded_rectangle([rx, bb - (bb - bt) * dec, rx + bwid, bb], radius=int(4 * s), fill=RED)
        _text_c(d, (rx + bwid / 2, cy + 36 * s), labels[1], font(11 * s), DIM)


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")


def start_time_from_name(path):
    m = _TS_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def default_output_path(path):
    root, _ = os.path.splitext(path)
    return root + "_overlay.mp4"


def _load(in_path):
    telemetry = read_frame_telemetry(in_path)
    if not any(telemetry):
        raise RuntimeError(
            "No Tesla telemetry found in this file.\n\n"
            "Data is only embedded in Dashcam clips (Saved/Recent, not Sentry) "
            "recorded on firmware 2025.44.25 or later with HW3 or newer, and may "
            "be missing while parked.")
    w, h, dur = probe(in_path)
    n = len(telemetry)
    fps = n / dur if dur > 0 else 36.0
    return telemetry, w, h, fps


def _renderers(in_path, telemetry, w, h, fps, units, position, show_gps, driving_path, horizon,
               hardware="auto", show_hardware=True, details=None):
    hw = resolve_hardware(hardware, w, h)
    overlay = OverlayRenderer(w, h, units=units, position=position, show_gps=show_gps,
                              start_time=start_time_from_name(in_path), fps=fps, telemetry=telemetry,
                              hardware_label=hw if show_hardware else None, details=details)
    path = None
    if driving_path and is_front_camera(in_path):
        path = DrivingPath(telemetry, w, h, fps, horizon=horizon, hardware=hw)
    return overlay, path


_hw_cache = {}


def clip_hardware(in_path):
    """(detected hardware or None, width, height) for a clip, from its frame size."""
    if in_path not in _hw_cache:
        w, h, _ = probe(in_path)
        if len(_hw_cache) > 200:
            _hw_cache.clear()
        _hw_cache[in_path] = (detect_hardware(w, h), w, h)
    return _hw_cache[in_path]


_frame_cache = {}


def set_steer_flip(flip):
    global STEER_SIGN
    STEER_SIGN = -1 if flip else 1


def preview(in_path, at=0.5, units="mph", position="bottom", show_gps=False,
            driving_path=False, horizon=DEFAULT_HORIZON, flip_steering=False, frame_hook=None,
            hardware="auto", show_hardware=True, details=None, _cache={}):
    """Return a PIL image of one overlaid frame, `at` = 0..1 through the clip."""
    if in_path not in _cache:
        _cache.clear()
        _cache[in_path] = _load(in_path)
    set_steer_flip(flip_steering)
    telemetry, w, h, fps = _cache[in_path]
    n = len(telemetry)
    # prefer a moment where the car is moving
    idx = int(at * (n - 1))
    for k in list(range(idx, n)) + list(range(idx, -1, -1)):
        if telemetry[k] and abs(telemetry[k]["vehicle_speed_mps"]) > 2:
            idx = k
            break
    key = (in_path, idx)
    raw = _frame_cache.get(key)
    if raw is None:
        p = subprocess.run([ffmpeg_exe(), "-v", "error", "-ss", f"{max(0.0, idx / fps - 2):.3f}", "-i", in_path,
                            "-map", "0:v:0", "-vf", f"select=gte(n\\,{int(min(idx, 2 * fps))})",
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, creationflags=_NOWINDOW)
        if len(p.stdout) < w * h * 3:
            raise RuntimeError("Could not read a frame for the preview.")
        raw = p.stdout[:w * h * 3]
        if len(_frame_cache) > 12:
            _frame_cache.clear()
        _frame_cache[key] = raw
    img = Image.frombytes("RGB", (w, h), raw)
    overlay, path = _renderers(in_path, telemetry, w, h, fps, units, position, show_gps, driving_path, horizon,
                               hardware, show_hardware, details)
    last = next((telemetry[k] for k in range(idx, -1, -1) if telemetry[k]), None)
    if path:
        path.draw(img, idx)
    if frame_hook:
        frame_hook(img, idx)
    overlay.render(img, last, idx)
    return img


def convert(in_path, out_path=None, units="mph", position="bottom", show_gps=False,
            driving_path=False, horizon=DEFAULT_HORIZON, flip_steering=False, frame_hook=None,
            hardware="auto", show_hardware=True, details=None, progress=None, cancel=None):
    """
    Burn the telemetry overlay into a new MP4. progress(fraction) is called as
    frames are written; cancel() returning True stops early. Returns out_path.
    """
    out_path = out_path or default_output_path(in_path)
    set_steer_flip(flip_steering)
    telemetry, w, h, fps = _load(in_path)
    n = len(telemetry)
    renderer, path = _renderers(in_path, telemetry, w, h, fps, units, position, show_gps,
                                driving_path, horizon, hardware, show_hardware, details)
    ff = ffmpeg_exe()
    dec = subprocess.Popen(
        [ff, "-v", "error", "-i", in_path, "-map", "0:v:0", "-fps_mode", "passthrough",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NOWINDOW)
    enc = subprocess.Popen(
        [ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
         "-framerate", f"{fps:.6f}", "-i", "-",
         *encoder_args(in_path, w, h), "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", out_path],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NOWINDOW)

    frame_bytes = w * h * 3
    idx = 0
    last = None
    cancelled = False
    try:
        while True:
            buf = dec.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            img = Image.frombuffer("RGB", (w, h), buf, "raw", "RGB", 0, 1).copy()
            t = telemetry[idx] if idx < n else None
            if t is not None:
                last = t
            if path:
                path.draw(img, idx)
            if frame_hook:
                frame_hook(img, idx)
            renderer.render(img, last, idx)
            enc.stdin.write(img.tobytes())
            idx += 1
            if progress and idx % 5 == 0:
                progress(min(idx / n, 1.0))
            if cancel and cancel():
                cancelled = True
                break
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
        dec.stdout.close()
        dec.kill()
        err = enc.stderr.read().decode(errors="replace")
        enc.wait()
        dec.wait()
    if cancelled:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError("Cancelled")
    if enc.returncode != 0 or idx == 0:
        raise RuntimeError("Encoding failed:\n" + err.strip()[-800:])
    if progress:
        progress(1.0)
    return out_path
