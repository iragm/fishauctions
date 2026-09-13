# Self-hosted vendor libraries

Third-party JS/CSS is vendored under `auctions/static/js/vendor/` and `auctions/static/css/vendor/`
(fonts in `auctions/static/fonts/`) rather than pulled from a CDN at runtime. `download_vendor_resources.sh`
is the source of truth for versions and download URLs — don't duplicate them here, they will drift.

Google Maps (needs an API key) and Google Analytics are the only libraries still loaded externally.

## Updating

```bash
./download_vendor_resources.sh
docker exec -it django python3 manage.py collectstatic --no-input
docker compose restart web nginx
```

`.github/workflows/weekly-dependency-update.yml` runs this every Monday and opens a PR.

## Adding a library

Add a `curl` block to `download_vendor_resources.sh`, run it, reference the file with
`{% static 'js/vendor/<file>' %}` / `{% static 'css/vendor/<file>' %}` in the template.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Vendor file missing | `./download_vendor_resources.sh` then `collectstatic --no-input` |
| JS errors after an update | Check the browser console; clear the static cache; confirm the template's filename matches what the script downloaded |
