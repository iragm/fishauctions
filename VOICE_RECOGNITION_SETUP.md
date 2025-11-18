# Voice Recognition Self-Hosted Resources Setup

## Overview

To enable voice recognition with SharedArrayBuffer support, all external resources must be self-hosted to achieve cross-origin isolation (COOP + COEP headers).

## Quick Start

```bash
# 1. Download all vendor resources
./download_vendor_resources.sh

# 2. Collect static files
docker exec -it django python3 manage.py collectstatic --no-input

# 3. Restart services
docker compose restart web nginx

# 4. Test voice recognition
# Navigate to any auction's "Set lot winners" page
# Click "Start Voice Control" button
```

## What Was Changed

### 1. Base Template (`auctions/templates/base.html`)
- Added `{% block analytics %}` to allow disabling Google Analytics
- Added `{% block ads %}` to allow disabling Google AdSense
- Added `{% block core_css %}` to allow overriding Bootstrap/Icons CSS
- Added `{% block core_js %}` to allow overriding Bootstrap JS

### 2. Set Lot Winners Template (`auctions/templates/auctions/dynamic_set_lot_winner.html`)
- Overrides `analytics` block (disabled)
- Overrides `ads` block (disabled)
- Overrides `core_css` block (uses self-hosted Bootstrap + Icons)
- Overrides `core_js` block (uses self-hosted Bootstrap bundle)
- Uses self-hosted Vosklet library instead of CDN

### 3. Self-Hosted Resources
Located in `auctions/static/`:
- `js/vendor/Vosklet.js` - Voice recognition library (~50KB)
- `js/vendor/bootstrap.bundle.min.js` - Bootstrap + Popper (~80KB)
- `css/vendor/bootstrap.min.css` - Darkly theme (~200KB)
- `css/vendor/bootstrap-icons.min.css` - Icons CSS (~90KB)
- `fonts/bootstrap-icons.woff2` - Icons font (~100KB)
- `fonts/bootstrap-icons.woff` - Icons font fallback (~150KB)

**Total: ~670KB**

## Why This Is Necessary

### SharedArrayBuffer Requirements

SharedArrayBuffer is required by Vosklet for WebAssembly memory. To use SharedArrayBuffer, the page must be **cross-origin isolated**:

1. **COOP (Cross-Origin-Opener-Policy):** `same-origin-allow-popups`
   - Isolates the browsing context
   - Allows OAuth popups (Google login)

2. **COEP (Cross-Origin-Embedder-Policy):** `require-corp`
   - Requires all embedded resources to opt-in to being loaded
   - Resources must either be:
     - Same-origin (self-hosted), OR
     - Served with `Cross-Origin-Resource-Policy: cross-origin` header

### The Problem with CDNs

Most CDNs don't serve resources with the required CORS headers:
- jsDelivr, cdnjs, Google APIs - Missing CORP headers
- Google Analytics, Google Ads - Cannot be cross-origin isolated

### The Solution

**Option 1: Self-Host Everything** ✅ (Implemented)
- Download and serve all resources from our domain
- Full control over CORS headers
- Privacy benefits (no external tracking)
- Offline capable

**Option 2: Web Speech API** ❌ (Not Implemented)
- Would send audio to Google servers
- Privacy concerns for auction data
- Requires internet connection

## Files That Need to be Downloaded

Run `./download_vendor_resources.sh` to download:

1. **Vosklet.js**
   - Source: https://cdn.jsdelivr.net/gh/msqr1/Vosklet@1.2.1/Examples/Vosklet.js
   - Destination: `auctions/static/js/vendor/Vosklet.js`

2. **Bootstrap Bundle**
   - Source: https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js
   - Destination: `auctions/static/js/vendor/bootstrap.bundle.min.js`

3. **Bootstrap CSS (Darkly)**
   - Source: https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css
   - Destination: `auctions/static/css/vendor/bootstrap.min.css`

4. **Bootstrap Icons CSS**
   - Source: https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css
   - Destination: `auctions/static/css/vendor/bootstrap-icons.min.css`
   - **Note:** Font paths are automatically updated to point to self-hosted fonts

5. **Bootstrap Icons Fonts**
   - Source: https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2
   - Destination: `auctions/static/fonts/bootstrap-icons.woff2`
   
   - Source: https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff
   - Destination: `auctions/static/fonts/bootstrap-icons.woff`

## Manual Download (if script fails)

```bash
# Create directories
mkdir -p auctions/static/js/vendor
mkdir -p auctions/static/css/vendor
mkdir -p auctions/static/fonts

# Download JS files
curl -L -o auctions/static/js/vendor/Vosklet.js \
  "https://cdn.jsdelivr.net/gh/msqr1/Vosklet@1.2.1/Examples/Vosklet.js"

curl -L -o auctions/static/js/vendor/bootstrap.bundle.min.js \
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"

# Download CSS files
curl -L -o auctions/static/css/vendor/bootstrap.min.css \
  "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css"

curl -L -o auctions/static/css/vendor/bootstrap-icons.min.css \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"

# Download fonts
curl -L -o auctions/static/fonts/bootstrap-icons.woff2 \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2"

curl -L -o auctions/static/fonts/bootstrap-icons.woff \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff"

# Update font paths in CSS (Linux)
sed -i 's|url("./fonts/bootstrap-icons.woff2")|url("/static/fonts/bootstrap-icons.woff2")|g' \
  auctions/static/css/vendor/bootstrap-icons.min.css
sed -i 's|url("./fonts/bootstrap-icons.woff")|url("/static/fonts/bootstrap-icons.woff")|g' \
  auctions/static/css/vendor/bootstrap-icons.min.css

# For macOS, use sed -i '' instead of sed -i
```

## Verification

### 1. Check Downloaded Files

```bash
ls -lh auctions/static/js/vendor/
ls -lh auctions/static/css/vendor/
ls -lh auctions/static/fonts/
```

All files should be present and non-zero size.

### 2. Check Static Files After collectstatic

```bash
docker exec -it django python3 manage.py collectstatic --no-input
ls -lh staticfiles/js/vendor/
ls -lh staticfiles/css/vendor/
ls -lh staticfiles/fonts/
```

### 3. Check Resources are Accessible

```bash
curl -I http://localhost/static/js/vendor/Vosklet.js
curl -I http://localhost/static/js/vendor/bootstrap.bundle.min.js
curl -I http://localhost/static/css/vendor/bootstrap.min.css
curl -I http://localhost/static/css/vendor/bootstrap-icons.min.css
curl -I http://localhost/static/fonts/bootstrap-icons.woff2
```

All should return `200 OK` with proper CORS headers:
- `Cross-Origin-Opener-Policy: same-origin-allow-popups`
- `Cross-Origin-Embedder-Policy: require-corp`
- `Cross-Origin-Resource-Policy: cross-origin`

### 4. Check SharedArrayBuffer is Available

Open browser console on the "Set lot winners" page:
```javascript
console.log(typeof SharedArrayBuffer); // Should print "function"
```

If it prints "undefined", cross-origin isolation is not working.

### 5. Test Voice Recognition

1. Navigate to any auction's "Set lot winners" page
2. Click "Start Voice Control" button
3. Allow microphone access when prompted
4. Status should show "Listening..."
5. Speak: "lot 123 sold to bidder 5 for 10 dollars"
6. Form should auto-fill and submit

## Troubleshooting

### SharedArrayBuffer is undefined

**Cause:** Cross-origin isolation not working

**Check:**
1. Are analytics/ads disabled? Look at page source for `<script>` tags
2. Are all resources self-hosted? Check Network tab in DevTools
3. Are COOP/COEP headers present? Check Response Headers in DevTools
4. Are there mixed content warnings? All resources must be HTTPS in production

### Voice recognition fails to initialize

**Cause:** Vosklet library or Vosk model not loading

**Check:**
1. Is Vosklet.js downloaded and not empty?
2. Is the Vosk model installed? (See `auctions/static/models/vosk/README.md`)
3. Check browser console for errors
4. Check Network tab for 404 or 403 errors

### Icons not displaying

**Cause:** Font paths incorrect in bootstrap-icons.min.css

**Fix:**
```bash
# Check font paths in CSS file
grep 'url(' auctions/static/css/vendor/bootstrap-icons.min.css

# Should show:
# url("/static/fonts/bootstrap-icons.woff2")
# url("/static/fonts/bootstrap-icons.woff")

# If not, run the download script again or manually update the paths
```

### Bootstrap styling broken

**Cause:** CSS not loading or version mismatch

**Check:**
1. Is bootstrap.min.css downloaded and not empty?
2. Does it match the Darkly theme from Bootswatch?
3. Is the Bootstrap JS bundle version compatible (5.3.3)?

## Impact on Other Pages

**Good news:** Only the "Set lot winners" page is affected!

Other pages continue to use CDN resources and have analytics/ads enabled. The template blocks allow selective overriding only where needed.

## Future Considerations

### Adding Voice Recognition to Other Pages

To add voice recognition to another page:

1. Override the same blocks in that page's template:
   ```django
   {% block analytics %}{% endblock %}
   {% block ads %}{% endblock %}
   {% block core_css %}
   <link rel="stylesheet" href="{% static 'css/vendor/bootstrap.min.css' %}">
   <link rel="stylesheet" href="{% static 'css/vendor/bootstrap-icons.min.css' %}">
   {% endblock %}
   {% block core_js %}
   <script src="{% static 'js/vendor/bootstrap.bundle.min.js' %}"></script>
   {% endblock %}
   ```

2. Include Vosklet:
   ```django
   {% block extra_js %}
   <script src="{% static 'js/vendor/Vosklet.js' %}"></script>
   {% endblock %}
   ```

### Updating Versions

To update Bootstrap or other libraries:

1. Update URLs in `download_vendor_resources.sh`
2. Run the download script
3. Run `collectstatic`
4. Test thoroughly

### Alternative: Use Web Speech API

If the complexity of self-hosting becomes an issue, consider using the browser's native Web Speech API:

**Pros:**
- No external libraries needed
- No SharedArrayBuffer requirement
- Simpler implementation

**Cons:**
- Sends audio to Google servers
- Requires internet connection
- Privacy concerns for auction data
- Less control over recognition

## Support

For issues or questions about this implementation, see:
- `auctions/static/models/vosk/VOICE_RECOGNITION.md` - Voice recognition usage guide
- `auctions/static/models/vosk/README.md` - Vosk model installation
- GitHub issue #502
