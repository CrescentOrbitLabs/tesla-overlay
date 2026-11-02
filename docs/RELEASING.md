# Releasing

## First push

Create an empty repository on GitHub (no README or license, since this folder already has them), then from this folder:

```
git init -b main
git add .
git commit -m "Tesla Overlay 1.0.0"
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

In the repository's Settings > General, upload `docs/images/social-preview.png` as the Social preview image, so shared links show that banner. On the repository's main page, click the gear next to About to add the description and topics from `docs/promo-copy.md`.

## Publishing a release

Option 1, let GitHub build it. Tag the version and push the tag:

```
git tag v1.0.0
git push origin v1.0.0
```

The workflow builds the Windows and Mac apps on GitHub's own Windows and Mac machines, adds `SHA256SUMS.txt`, and publishes the release using `docs/release-notes/v1.0.0.md`. Progress shows on the Actions tab; it takes about ten minutes.

Option 2, upload ready-made files. On GitHub, go to Releases > Draft a new release, create the tag `v1.0.0`, paste `docs/release-notes/v1.0.0.md` as the description (replace `OWNER/REPO` with your username and repository name), and attach `TeslaOverlay-1.0.0-windows.zip`, `TeslaOverlay-1.0.0-mac.zip` and `SHA256SUMS.txt`.

## Next versions

Update `VERSION`, `APP_VERSION` in `tesla_overlay.py`, the four version numbers in `assets/windows_version_info.txt`, and the user agent in `tesla_map.py`. Add a section to `CHANGELOG.md` and a file in `docs/release-notes/`, then tag the new version.
