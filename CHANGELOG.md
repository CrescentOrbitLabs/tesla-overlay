# Changelog

## 1.0.0 (2026-11-01)

First public release, for Windows and macOS.

- Reads the telemetry Tesla embeds in dashcam clips (firmware 2025.44.25 or later, HW3 or newer) and burns an overlay into a new copy of the video.
- Speed panel: speed (mph or km/h), gear, steering angle, turn signals, FSD / Autosteer / TACC badge, date, time and heading, with optional GPS coordinates.
- Pedal bars that show your accelerator and brake while you drive, and the car's own speeding up and slowing down while FSD, Autosteer or TACC is driving.
- FSD-style driving path on front camera clips, built from the recorded heading: solid while driving, faded while slowing, with a stop line where the car stops. Camera geometry measured for HW4, with an adjustable path height.
- Live heading-up mini map in the corner of the video.
- Route map image and animated map video exports, using OpenStreetMap.
- Liquid glass interface with System, Light and Dark themes, live preview, batch processing and remembered settings.
- Reliable map downloads: one kept-alive connection, retries, and a fallback to the system's own downloader.
