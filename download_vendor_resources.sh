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
echo "2. Downloading Bootstrap 5.3.3 bundle (includes Popper)..."
curl -L -o auctions/static/js/vendor/bootstrap.bundle.min.js \
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
echo "✓ bootstrap.bundle.min.js downloaded"

echo ""
echo "3. Downloading Bootstrap Darkly theme CSS..."
curl -L -o auctions/static/css/vendor/bootstrap.min.css \
  "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/darkly/bootstrap.min.css"
echo "✓ bootstrap.min.css downloaded"

echo ""
echo "4. Downloading Bootstrap Icons CSS..."
curl -L -o auctions/static/css/vendor/bootstrap-icons.min.css \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"
echo "✓ bootstrap-icons.min.css downloaded"

echo ""
echo "5. Downloading Bootstrap Icons fonts..."
curl -L -o auctions/static/fonts/bootstrap-icons.woff2 \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2"
echo "✓ bootstrap-icons.woff2 downloaded"

curl -L -o auctions/static/fonts/bootstrap-icons.woff \
  "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff"
echo "✓ bootstrap-icons.woff downloaded"

echo ""
echo "6. Updating font paths in Bootstrap Icons CSS..."
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
echo "======================================================================"
echo "All resources downloaded successfully!"
echo ""
echo "Next steps:"
echo "1. Run: docker exec -it django python3 manage.py collectstatic --no-input"
echo "2. Run: docker compose restart web nginx"
echo "3. Test the voice recognition feature"
echo ""
echo "Downloaded files:"
ls -lh auctions/static/js/vendor/
ls -lh auctions/static/css/vendor/
ls -lh auctions/static/fonts/bootstrap-icons.*
echo ""
echo "Total size:"
du -sh auctions/static/js/vendor/
du -sh auctions/static/css/vendor/
du -sh auctions/static/fonts/
