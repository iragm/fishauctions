#!/bin/bash
# Download script for self-hosted resources required for voice recognition
# This enables cross-origin isolation for SharedArrayBuffer support

set -e  # Exit on error

echo "Downloading self-hosted resources for voice recognition..."
echo "======================================================================"

# Navigate to project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Create directories if they don't exist
mkdir -p auctions/static/js/vendor
mkdir -p auctions/static/css/vendor
mkdir -p auctions/static/fonts

echo ""
echo "1. Downloading Vosklet library..."
curl -L -o auctions/static/js/vendor/Vosklet.js \
  "https://cdn.jsdelivr.net/gh/msqr1/Vosklet@1.2.1/Examples/Vosklet.js"
echo "✓ Vosklet.js downloaded"

echo ""
echo "1b. Downloading Vosklet WASM module..."
curl -L -o auctions/static/js/vendor/Vosklet.wasm \
  "https://cdn.jsdelivr.net/gh/msqr1/Vosklet@1.2.1/Examples/Vosklet.wasm"
echo "✓ Vosklet.wasm downloaded"

echo ""
echo "2. Downloading jQuery 3.5.1..."
curl -L -o auctions/static/js/vendor/jquery.min.js \
  "https://ajax.googleapis.com/ajax/libs/jquery/3.5.1/jquery.min.js"
echo "✓ jquery.min.js downloaded"

echo ""
echo "3. Downloading Bootstrap 5.3.3 bundle (includes Popper)..."
curl -L -o auctions/static/js/vendor/bootstrap.bundle.min.js \
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
echo "✓ bootstrap.bundle.min.js downloaded"

echo ""
echo "4. Downloading Bootstrap Darkly theme CSS..."
curl -L -o auctions/static/css/vendor/bootstrap.min.css \
  "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css"
echo "✓ bootstrap.min.css downloaded"

echo ""
echo "5. Downloading Bootstrap Icons CSS..."
curl -L -o auctions/static/css/vendor/bootstrap-icons.min.css \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"
echo "✓ bootstrap-icons.min.css downloaded"

echo ""
echo "6. Downloading Bootstrap Icons fonts..."
curl -L -o auctions/static/fonts/bootstrap-icons.woff2 \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2"
echo "✓ bootstrap-icons.woff2 downloaded"

curl -L -o auctions/static/fonts/bootstrap-icons.woff \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff"
echo "✓ bootstrap-icons.woff downloaded"

echo ""
echo "7. Updating font paths in Bootstrap Icons CSS..."
# Update the @font-face URLs to point to self-hosted fonts
if [[ "$OSTYPE" == "darwin"* ]]; then
  # macOS
  sed -i '' 's|url("./fonts/bootstrap-icons.woff2")|url("/static/fonts/bootstrap-icons.woff2")|g' auctions/static/css/vendor/bootstrap-icons.min.css
  sed -i '' 's|url("./fonts/bootstrap-icons.woff")|url("/static/fonts/bootstrap-icons.woff")|g' auctions/static/css/vendor/bootstrap-icons.min.css
else
  # Linux
  sed -i 's|url("./fonts/bootstrap-icons.woff2")|url("/static/fonts/bootstrap-icons.woff2")|g' auctions/static/css/vendor/bootstrap-icons.min.css
  sed -i 's|url("./fonts/bootstrap-icons.woff")|url("/static/fonts/bootstrap-icons.woff")|g' auctions/static/css/vendor/bootstrap-icons.min.css
fi
echo "✓ Font paths updated"

echo ""
echo "8. Downloading Popper.js 1.11.0..."
curl -L -o auctions/static/js/vendor/popper.min.js \
  "https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.11.0/umd/popper.min.js"
echo "✓ popper.min.js downloaded"

echo ""
echo "9. Downloading Select2 4.0.13 (latest version)..."
curl -L -o auctions/static/css/vendor/select2.min.css \
  "https://cdnjs.cloudflare.com/ajax/libs/select2/4.0.13/css/select2.min.css"
echo "✓ select2.min.css downloaded"

curl -L -o auctions/static/js/vendor/select2.min.js \
  "https://cdnjs.cloudflare.com/ajax/libs/select2/4.0.13/js/select2.min.js"
echo "✓ select2.min.js downloaded"

echo ""
echo "10. Downloading Select2 Bootstrap theme..."
curl -L -o auctions/static/css/vendor/select2-bootstrap.min.css \
  "https://cdnjs.cloudflare.com/ajax/libs/select2-bootstrap-theme/0.1.0-beta.10/select2-bootstrap.min.css"
echo "✓ select2-bootstrap.min.css downloaded"

echo ""
echo "11. Downloading Chart.js 2.9.3..."
curl -L -o auctions/static/js/vendor/Chart.min.js \
  "https://cdn.jsdelivr.net/npm/chart.js@2.9.3/dist/Chart.min.js"
echo "✓ Chart.min.js downloaded"

echo ""
echo "12. Downloading Bootstrap 4.5.2 (for print.html)..."
curl -L -o auctions/static/js/vendor/bootstrap-4.5.2.min.js \
  "https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/js/bootstrap.min.js"
echo "✓ bootstrap-4.5.2.min.js downloaded"

echo ""
echo "13. Downloading jQuery 3.6.1 (for generic_admin_form.html)..."
curl -L -o auctions/static/js/vendor/jquery-3.6.1.min.js \
  "https://ajax.googleapis.com/ajax/libs/jquery/3.6.1/jquery.min.js"
echo "✓ jquery-3.6.1.min.js downloaded"

echo ""
echo "======================================================================"
echo "All resources downloaded successfully!"
echo ""
echo "Next steps:"
echo "1. Run: docker exec -it django python3 manage.py collectstatic --no-input"
echo "2. Run: docker compose restart web nginx"
echo "3. Test the application with self-hosted resources"
echo ""
echo "Downloaded files:"
ls -lh auctions/static/js/vendor/
ls -lh auctions/static/css/vendor/
ls -lh auctions/static/fonts/bootstrap-icons.* 2>/dev/null || true
echo ""
echo "Total size:"
du -sh auctions/static/js/vendor/
du -sh auctions/static/css/vendor/
du -sh auctions/static/fonts/ 2>/dev/null || true
