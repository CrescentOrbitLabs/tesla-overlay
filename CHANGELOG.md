# Changelog

## 1.0.3 (2026-11-07)

- New Advanced menu in the Overlay card for details the clips don't record: speed profile (Sloth, Chill, Standard, Hurry, Mad Max), car model (every Tesla model and trim), and FSD version (every release from v9 to v14.3, including the HW3 Lite builds, or any exact version typed in). Anything set is shown in a strip above the time on the video.
- Command line: `--model`, `--fsd` and `--profile`.

## 1.0.2 (2026-11-07)

- New Hardware menu (Auto, HW3, HW4). Auto detects the car's hardware from the clip's video size, and the choice sets the camera field of view used for the driving path.
- The hardware version is shown next to the time in the video, with a switch to hide it.
- Command line: `--hw=auto|hw3|hw4` and `--no-hw-label`.

## 1.0.1 (2026-11-02)

- Tesla Overlay is now licensed under the PolyForm Strict License 1.0.0 instead of the MIT License. It stays free for personal and noncommercial use; modifying or redistributing it is no longer permitted. Copies of 1.0.0 already downloaded keep their original license.
- The License panel's third-party notices tab is now labeled Third party.

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
