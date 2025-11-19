# Self-Hosting Resources for Voice Recognition

This document lists all the resources that need to be downloaded and self-hosted to enable cross-origin isolation for SharedArrayBuffer support.

## Required Downloads

### 1. Vosklet Library
**Source:** https://cdn.jsdelivr.net/gh/msqr1/Vosklet@1.2.1/Examples/Vosklet.js
**Destination:** `auctions/static/js/vendor/Vosklet.js`
**Size:** ~50KB

### 2. Bootstrap 5.3.3 JavaScript Bundle
**Source:** https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js
**Destination:** `auctions/static/js/vendor/bootstrap.bundle.min.js`
**Size:** ~80KB
**Note:** Includes Popper.js

### 3. Bootstrap 5.3.3 CSS (Darkly Theme)
**Source:** https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css
**Destination:** `auctions/static/css/vendor/bootstrap.min.css`
**Size:** ~200KB

### 4. Bootstrap Icons Font
**Source:** https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css
**Destination:** `auctions/static/css/vendor/bootstrap-icons.min.css`
**Size:** ~90KB

**Source:** https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff
**Destination:** `auctions/static/fonts/bootstrap-icons.woff`
**Size:** ~150KB

**Source:** https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2
**Destination:** `auctions/static/fonts/bootstrap-icons.woff2`
**Size:** ~100KB

### 5. jQuery (Already Self-Hosted)
The site already uses jQuery from various sources. Ensure all pages use the self-hosted version.

## Download Commands

```bash
# Navigate to the project root
cd /path/to/fishauctions

# Create vendor directories
mkdir -p auctions/static/js/vendor
mkdir -p auctions/static/css/vendor
mkdir -p auctions/static/fonts

# Download JavaScript files
curl -L -o auctions/static/js/vendor/Vosklet.js \
  "https://cdn.jsdelivr.net/gh/msqr1/Vosklet@1.2.1/Examples/Vosklet.js"

curl -L -o auctions/static/js/vendor/bootstrap.bundle.min.js \
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"

# Download CSS files
curl -L -o auctions/static/css/vendor/bootstrap.min.css \
  "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css"

curl -L -o auctions/static/css/vendor/bootstrap-icons.min.css \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"

# Download font files
curl -L -o auctions/static/fonts/bootstrap-icons.woff \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff"

curl -L -o auctions/static/fonts/bootstrap-icons.woff2 \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2"
```

## Post-Download Steps

1. **Update CSS Font Paths**
   Edit `auctions/static/css/vendor/bootstrap-icons.min.css` and update font-face URLs:
   ```css
   @font-face {
     font-family: "bootstrap-icons";
     src: url("/static/fonts/bootstrap-icons.woff2") format("woff2"),
          url("/static/fonts/bootstrap-icons.woff") format("woff");
   }
   ```

2. **Run Django collectstatic**
   ```bash
   docker exec -it django python3 manage.py collectstatic --no-input
   ```

3. **Restart containers**
   ```bash
   docker compose restart web nginx
   ```

## Verification

After deployment, verify the resources are loading correctly:

```bash
# Check Vosklet
curl -I http://your-domain/static/js/vendor/Vosklet.js

# Check Bootstrap
curl -I http://your-domain/static/js/vendor/bootstrap.bundle.min.js
curl -I http://your-domain/static/css/vendor/bootstrap.min.css

# Check Icons
curl -I http://your-domain/static/css/vendor/bootstrap-icons.min.css
curl -I http://your-domain/static/fonts/bootstrap-icons.woff2
```

All should return 200 OK with proper CORS headers.

## Total Size Estimate

- JavaScript: ~130KB
- CSS: ~290KB
- Fonts: ~250KB
- **Total: ~670KB** (significantly less than the 5MB estimate)
