# Self-Hosted Vendor Libraries

This document describes the self-hosted vendor libraries used in the Fish Auctions project and how to manage them.

## Overview

All third-party JavaScript and CSS libraries are now self-hosted in the `auctions/static/js/vendor/` and `auctions/static/css/vendor/` directories instead of being loaded from external CDNs. This approach provides several benefits:

- **Performance**: Resources are served from the same domain, reducing DNS lookups and connection overhead
- **Reliability**: No dependency on external CDN availability
- **Privacy**: No third-party tracking or data collection via CDN providers
- **Offline Development**: Full functionality without internet access
- **Security**: Complete control over library versions and content

## Current Self-Hosted Libraries

### JavaScript Libraries

Located in `auctions/static/js/vendor/`:

- **jQuery 3.5.1** (`jquery.min.js`) - Used for DOM manipulation and AJAX requests
- **jQuery 3.6.1** (`jquery-3.6.1.min.js`) - Newer version for specific components
- **Bootstrap 5.3.3** (`bootstrap.bundle.min.js`) - Includes Popper.js for tooltips/popovers
- **Bootstrap 4.5.2** (`bootstrap-4.5.2.min.js`) - Legacy version for print.html
- **Select2 4.0.13** (`select2.min.js`) - Enhanced select boxes
- **Chart.js 2.9.3** (`Chart.min.js`) - Data visualization and charting
- **Vosklet** (`Vosklet.js`) - Voice recognition library

### CSS Libraries

Located in `auctions/static/css/vendor/`:

- **Bootstrap Darkly 5.3.3** (`bootstrap.min.css`) - Bootswatch dark theme
- **Bootstrap Icons 1.11.3** (`bootstrap-icons.min.css`) - Icon font
- **Select2 4.0.13** (`select2.min.css`) - Select2 styling
- **Select2 Bootstrap Theme** (`select2-bootstrap.min.css`) - Bootstrap integration for Select2

### Fonts

Located in `auctions/static/fonts/`:

- **Bootstrap Icons** (`bootstrap-icons.woff2`, `bootstrap-icons.woff`) - Icon font files

## Updating Vendor Libraries

### Automatic Updates (Recommended)

The weekly dependency update workflow automatically downloads the latest versions of all vendor libraries:

```bash
# This runs automatically every Monday at 05:00 UTC
# You can also trigger it manually from GitHub Actions
```

The workflow will:
1. Run `download_vendor_resources.sh` to fetch the latest versions
2. Create a pull request with the updates
3. Run tests to ensure everything still works

### Manual Updates

To manually update vendor libraries:

```bash
# Make the script executable
chmod +x download_vendor_resources.sh

# Run the download script
./download_vendor_resources.sh

# Collect static files (if Docker is running)
docker exec -it django python3 manage.py collectstatic --no-input

# Restart services
docker compose restart web nginx
```

## Template Usage

Templates reference self-hosted libraries using Django's static template tag:

```django
{% load static %}

<!-- JavaScript -->
<script src="{% static 'js/vendor/jquery.min.js' %}"></script>
<script src="{% static 'js/vendor/bootstrap.bundle.min.js' %}"></script>

<!-- CSS -->
<link rel="stylesheet" href="{% static 'css/vendor/bootstrap.min.css' %}">
<link rel="stylesheet" href="{% static 'css/vendor/bootstrap-icons.min.css' %}">
```

## Templates Updated

The following templates were updated to use self-hosted resources:

- `auctions/templates/base.html` - Core dependencies (jQuery, Bootstrap, Bootstrap Icons)
- `auctions/templates/ignore_categories.html` - Select2 library
- `auctions/templates/auction_stats.html` - Select2 and Chart.js
- `auctions/templates/user.html` - Chart.js
- `auctions/templates/dashboard_traffic.html` - Chart.js
- `auctions/templates/print.html` - jQuery and Bootstrap
- `auctions/templates/auctions/generic_admin_form.html` - jQuery

## External Services Retained

The following external services are still referenced because they require API keys or provide essential third-party services:

- **Google Maps API** - Requires API key, used for location mapping
- **Google Analytics** - Analytics tracking service

## Version Management

Library versions are specified in the `download_vendor_resources.sh` script. When updating:

1. Update the version number in the download URL
2. Test thoroughly to ensure compatibility
3. Update this documentation with the new version numbers

## Troubleshooting

### Missing Files

If vendor files are missing, run:

```bash
./download_vendor_resources.sh
docker exec -it django python3 manage.py collectstatic --no-input
```

### Version Mismatch

If you encounter JavaScript errors after an update:

1. Check browser console for specific errors
2. Verify template references match actual file names
3. Clear browser cache and Django static files cache
4. Restart the Django development server

### Download Script Fails

If the download script fails:

1. Check your internet connection
2. Verify the CDN URLs are still valid
3. Check for rate limiting by CDN providers
4. Try downloading individual files manually

## Adding New Libraries

To add a new vendor library:

1. Add download command to `download_vendor_resources.sh`
2. Run the script to download the file
3. Update templates to reference the new library
4. Add the library to this documentation
5. Test thoroughly
6. Commit both the script changes and the downloaded files

Example:

```bash
echo ""
echo "X. Downloading NewLibrary X.Y.Z..."
curl -L -o auctions/static/js/vendor/newlibrary.min.js \
  "https://cdn.example.com/newlibrary@X.Y.Z/dist/newlibrary.min.js"
echo "✓ newlibrary.min.js downloaded"
```

## Migration History

**December 2024**: Initial migration from CDN-hosted to self-hosted libraries

- Moved jQuery, Bootstrap, Bootstrap Icons, Select2, and Chart.js to self-hosting
- Created `download_vendor_resources.sh` script for easy updates
- Updated weekly dependency workflow to include vendor libraries
- Removed all CDN references except for Google services

## References

- [jQuery](https://jquery.com/)
- [Bootstrap](https://getbootstrap.com/)
- [Bootswatch](https://bootswatch.com/)
- [Bootstrap Icons](https://icons.getbootstrap.com/)
- [Select2](https://select2.org/)
- [Chart.js](https://www.chartjs.org/)
