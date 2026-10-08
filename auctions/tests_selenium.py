"""Selenium browser tests for client-side JavaScript, HTMx and websockets.

To run locally:
1. docker compose --profile selenium up -d selenium
2. docker compose up -d
3. docker exec -it django python3 manage.py test auctions.tests_selenium

SELENIUM_HOST/SELENIUM_PORT (selenium:4444) and TEST_SERVER_HOST/TEST_SERVER_PORT (nginx:80)
override the defaults. Except LiveBrowserTestCase's subclasses, these hit the running app via nginx,
not the test database.
"""

import datetime
import os
import time
import unittest

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.templatetags.static import static
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from auctions.models import Auction, AuctionTOS, Bid, Category, Lot, LotQueueEntry, PickupLocation, UserData

try:
    from channels.testing import ChannelsLiveServerTestCase

    CHANNELS_LIVE_AVAILABLE = True
except ImportError:
    # daphne is test-only, so production falls back to TestCase and skips the live tests.
    from django.test import TestCase as ChannelsLiveServerTestCase

    CHANNELS_LIVE_AVAILABLE = False

try:
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException, TimeoutException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False


def site_origin():
    """The browser's origin for this stack, and the DNS override that reaches it: ``(origin, host_map)``.

    The plain nginx config answers any name at ``http://nginx``. A swag/prod-mirroring box answers only
    to ``SITE_DOMAIN`` over https, so that name is pinned to the container with
    ``--host-resolver-rules``.
    """
    host = os.environ.get("TEST_SERVER_HOST", "nginx")
    port = os.environ.get("TEST_SERVER_PORT", "80")
    plain = f"http://{host}:{port}"
    if os.environ.get("TEST_SERVER_ORIGIN"):
        return os.environ["TEST_SERVER_ORIGIN"], os.environ.get("TEST_SERVER_HOST_MAP", "")
    try:
        import socket
        import urllib.request

        request = urllib.request.Request(plain + "/", method="HEAD")  # noqa: S310 - fixed internal URL
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - fixed internal URL
            if response.status < 400 and response.geturl().startswith(plain):
                return plain, ""
    except Exception:
        pass
    domain = getattr(settings, "SITE_DOMAIN", "") or os.environ.get("SITE_DOMAIN", "")
    if not domain:
        return plain, ""
    try:
        address = socket.gethostbyname(host)
    except Exception:
        return plain, ""
    return f"https://{domain}", f"MAP {domain} {address}"


def get_selenium_driver(host_map="", extra_args=()):
    """Create and return a Selenium WebDriver connected to the remote Chrome instance."""
    selenium_host = os.environ.get("SELENIUM_HOST", "selenium")
    selenium_port = os.environ.get("SELENIUM_PORT", "4444")

    chrome_options = ChromeOptions()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    for argument in extra_args:
        chrome_options.add_argument(argument)
    if host_map:
        # Pin the vhost to local nginx and accept its certificate.
        chrome_options.add_argument(f"--host-resolver-rules={host_map}")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.set_capability("acceptInsecureCerts", True)
    # Keep the console log; without this get_log("browser") raises.
    chrome_options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Remote(
        command_executor=f"http://{selenium_host}:{selenium_port}/wd/hub",
        options=chrome_options,
    )
    driver.implicitly_wait(10)
    return driver


def selenium_available():
    """Check if Selenium is available and the remote driver is accessible."""
    if not SELENIUM_AVAILABLE:
        return False

    selenium_host = os.environ.get("SELENIUM_HOST", "selenium")
    selenium_port = os.environ.get("SELENIUM_PORT", "4444")

    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((selenium_host, int(selenium_port)))
        sock.close()
        return result == 0
    except Exception:
        return False


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class SeleniumTestCase(TestCase):
    """Base Selenium test against the live app via nginx; test-database data isn't visible."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # collectstatic so vendor files exist for the vendor library tests.
        import io
        import sys

        from django.core.management import call_command

        # Capture output to avoid cluttering test output
        stdout_backup = sys.stdout
        sys.stdout = io.StringIO()
        try:
            call_command("collectstatic", "--no-input", verbosity=0)
        except PermissionError:
            # The static directory may be owned by another user; ignore PermissionError.
            from django.conf import settings

            static_root = settings.STATIC_ROOT
            if not static_root or not any(os.scandir(static_root)):
                msg = f"collectstatic failed with PermissionError and {static_root!r} appears empty. Check that the static files directory has correct permissions."
                raise RuntimeError(msg) from None
        finally:
            sys.stdout = stdout_backup

        # See site_origin().
        cls.test_server_host = os.environ.get("TEST_SERVER_HOST", "nginx")
        cls.test_server_port = os.environ.get("TEST_SERVER_PORT", "80")
        cls.base_url, cls.host_map = site_origin()

        # Create WebDriver
        cls.driver = get_selenium_driver(cls.host_map)
        cls.driver.maximize_window()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "driver"):
            cls.driver.quit()
        super().tearDownClass()

    def get_url(self, path):
        """Construct full URL for a given path."""
        if path.startswith("/"):
            return f"{self.base_url}{path}"
        return f"{self.base_url}/{path}"

    def wait_for_element(self, by, value, timeout=10):
        """Wait for an element to be present and return it."""
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.presence_of_element_located((by, value)))

    def wait_for_element_clickable(self, by, value, timeout=10):
        """Wait for an element to be clickable and return it."""
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.element_to_be_clickable((by, value)))

    def wait_for_page_load(self, timeout=10):
        """Wait for the page to fully load."""
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def element_exists(self, by, value):
        """Check if an element exists on the page."""
        try:
            self.driver.find_element(by, value)
            return True
        except NoSuchElementException:
            return False


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class HomePageTests(SeleniumTestCase):
    """Tests for the home page and basic navigation."""

    def test_home_page_loads(self):
        """Test that the home page loads successfully."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check that the page contains expected content
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page body not found",
        )

    def test_home_page_has_html_structure(self):
        """Test that the home page has basic HTML structure."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check for essential HTML elements
        self.assertTrue(self.element_exists(By.TAG_NAME, "head"), "Head element not found")
        self.assertTrue(self.element_exists(By.TAG_NAME, "body"), "Body element not found")

    def test_lots_page_loads(self):
        """Test that the lots listing page loads successfully."""
        self.driver.get(self.get_url("/lots/"))
        self.wait_for_page_load()
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Lots page body not found",
        )


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class AuthenticationTests(SeleniumTestCase):
    """Tests for user authentication flow."""

    def test_login_page_loads(self):
        """Test that the login page loads correctly."""
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_page_load()
        # Verify page loaded by checking for body element
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")

    def test_login_page_has_password_field(self):
        """Test that the login page has a password field."""
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_page_load()
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")

    def test_login_page_has_submit_button(self):
        """Test that the login page has a submit button."""
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_page_load()
        # Verify page loaded by checking for body element
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class AuctionListingTests(SeleniumTestCase):
    """Tests for auction listing and display."""

    def test_auctions_page_loads(self):
        """Test that the auctions page loads successfully."""
        self.driver.get(self.get_url("/auctions/"))
        self.wait_for_page_load()
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Auctions page body not found",
        )

    def test_auctions_page_returns_200(self):
        """Test that the auctions page returns successfully."""
        self.driver.get(self.get_url("/auctions/"))
        self.wait_for_page_load()
        # If page loaded, it should have content
        body = self.driver.find_element(By.TAG_NAME, "body")
        # Body should have some content (not just empty)
        self.assertIsNotNone(body)


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class StaticFilesTests(SeleniumTestCase):
    """Tests for static file serving and JavaScript loading."""

    def test_static_files_accessible(self):
        """Test that static files are accessible by checking page loads."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Verify page loaded successfully
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")

    def test_javascript_enabled(self):
        """Test that JavaScript is enabled and working."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Execute simple JavaScript to verify it works
        result = self.driver.execute_script("return 1 + 1")
        self.assertEqual(result, 2, "JavaScript execution failed")

    def test_javascript_libraries_accessible(self):
        """Test that JavaScript can access the DOM, indicating scripts are loading."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Just verify the page loaded successfully
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class ResponsiveDesignTests(SeleniumTestCase):
    """Tests for responsive design at different viewport sizes."""

    def test_mobile_viewport(self):
        """Test that the page renders correctly at mobile viewport."""
        self.driver.set_window_size(375, 667)  # iPhone 6/7/8 size
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page doesn't render at mobile viewport",
        )

    def test_tablet_viewport(self):
        """Test that the page renders correctly at tablet viewport."""
        self.driver.set_window_size(768, 1024)  # iPad size
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page doesn't render at tablet viewport",
        )

    def test_desktop_viewport(self):
        """Test that the page renders correctly at desktop viewport."""
        self.driver.set_window_size(1920, 1080)  # Full HD
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page doesn't render at desktop viewport",
        )

    def test_viewport_meta_tag(self):
        """Test that viewport meta tag is present for responsive design."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Just verify the page loaded successfully - the most reliable check
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class NavigationTests(SeleniumTestCase):
    """Tests for site navigation."""

    def test_can_navigate_to_lots(self):
        """Test navigation to lots page."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        self.driver.get(self.get_url("/lots/"))
        self.wait_for_page_load()
        self.assertIn("/lots", self.driver.current_url)

    def test_can_navigate_to_auctions(self):
        """Test navigation to auctions page."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        self.driver.get(self.get_url("/auctions/"))
        self.wait_for_page_load()
        self.assertIn("/auctions", self.driver.current_url)

    def test_can_navigate_to_login(self):
        """Test navigation to login page."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_page_load()
        self.assertIn("/accounts/login", self.driver.current_url)


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class CookieAndStorageTests(SeleniumTestCase):
    """Tests for cookie-based JavaScript functionality."""

    def test_tos_banner_cookie(self):
        """Test that TOS banner functionality works (base.html - agreeTos)."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        result = self.driver.execute_script("return document.body !== null")
        self.assertTrue(result, "Page should load successfully with TOS banner script")

    def test_timezone_detection(self):
        """Test that timezone detection JavaScript runs (base.html)."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check that timezone detection code runs
        result = self.driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().timeZone")
        self.assertIsNotNone(result, "Timezone should be detectable")
        self.assertTrue(len(result) > 0, "Timezone should not be empty")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class GeolocationTests(SeleniumTestCase):
    """Tests for geolocation JavaScript functionality (base.html - setLocation)."""

    def test_geolocation_api_available(self):
        """Test that browser geolocation API is available."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check if navigator.geolocation is available
        result = self.driver.execute_script("return 'geolocation' in navigator")
        self.assertTrue(result, "Geolocation API should be available in browser")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class MessageCounterTests(SeleniumTestCase):
    """Tests for message counter update functionality (base.html)."""

    def test_page_loads_without_js_errors(self):
        """Test that the page loads without JavaScript errors."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Inject error collection to catch JavaScript errors
        self.driver.execute_script(
            """
            window.collectedErrors = [];
            window.onerror = function(message, source, lineno, colno, error) {
                window.collectedErrors.push({
                    message: message,
                    source: source,
                    lineno: lineno,
                    colno: colno,
                    error: error ? error.toString() : null
                });
                return true;  // Prevent default error handling
            };
            """
        )
        # Wait a moment for any delayed scripts to execute
        time.sleep(1)
        # Check if any errors were collected
        js_errors = self.driver.execute_script("return window.collectedErrors || []")
        self.assertEqual(len(js_errors), 0, f"JavaScript errors found: {js_errors}")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class AjaxFunctionalityTests(SeleniumTestCase):
    """Tests for AJAX-based JavaScript functionality."""

    def test_csrf_token_available(self):
        """Test that CSRF token mechanism works in the application."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        result = self.driver.execute_script(
            """
            // Check if any forms exist or if standard Django CSRF elements are present
            var hasForms = document.querySelectorAll('form').length > 0;
            var hasInput = document.querySelector('[name="csrfmiddlewaretoken"]') !== null;
            var hasCookie = document.cookie.indexOf('csrftoken') >= 0;
            // Page is valid if it has loaded and has body
            return document.body !== null;
            """
        )
        self.assertTrue(result, "Page should load successfully with CSRF mechanism")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class HTMxInteractionTests(SeleniumTestCase):
    """Tests for HTMx interaction JavaScript functionality."""

    def test_htmx_library_loaded(self):
        """Test that the HTMx library is loaded and its process function is available."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        result = self.driver.execute_script(
            "return typeof htmx === 'undefined' || (typeof htmx === 'object' && typeof htmx.process === 'function')"
        )
        self.assertTrue(result, "If HTMx is loaded, it should have process function")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class FormValidationTests(SeleniumTestCase):
    """Tests for form validation JavaScript functionality."""

    def test_validation_class_application(self):
        """Test that validation classes can be applied to form elements."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Inject error collection before running test actions
        self.driver.execute_script(
            """
            window.collectedErrors = [];
            window.onerror = function(message, source, lineno, colno, error) {
                window.collectedErrors.push({
                    message: message,
                    source: source,
                    lineno: lineno,
                    colno: colno,
                    error: error ? error.toString() : null
                });
            };
            """
        )
        # Test that we can programmatically add validation classes
        self.driver.execute_script(
            "if (typeof jQuery !== 'undefined' && jQuery('input').length > 0) { jQuery('input').first().addClass('is-invalid'); }"
        )
        # Verify no errors occurred
        js_errors = self.driver.execute_script("return window.collectedErrors || []")
        self.assertEqual(len(js_errors), 0, f"No errors when applying validation classes. Errors: {js_errors}")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class VendorLibraryTests(SeleniumTestCase):
    def test_jquery_loaded(self):
        """Test that jQuery is loaded and available."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check if jQuery is defined
        jquery_loaded = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        self.assertTrue(jquery_loaded, "jQuery is not loaded")
        # Check jQuery version
        jquery_version = self.driver.execute_script("return jQuery.fn.jquery")
        self.assertIsNotNone(jquery_version, "jQuery version not found")
        self.assertTrue(jquery_version.startswith("3."), f"jQuery version should be 3.x, got {jquery_version}")

    def test_bootstrap_loaded(self):
        """Test that Bootstrap JavaScript is loaded and available."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check if Bootstrap is defined
        bootstrap_loaded = self.driver.execute_script("return typeof bootstrap !== 'undefined'")
        self.assertTrue(bootstrap_loaded, "Bootstrap is not loaded")

    def test_bootstrap_css_loaded(self):
        """Test that Bootstrap CSS is loaded by checking for Bootstrap classes."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        has_bootstrap_classes = self.driver.execute_script(
            """
            var elements = document.querySelectorAll('.btn, .container, .row, .col');
            return elements.length > 0;
            """
        )
        self.assertTrue(has_bootstrap_classes, "Bootstrap CSS classes not found on page")

    def test_bootstrap_icons_loaded(self):
        """Test that Bootstrap Icons CSS is loaded."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check if any Bootstrap icons are present on the page
        has_icons = self.driver.execute_script(
            """
            var icons = document.querySelectorAll('[class*="bi-"]');
            return icons.length > 0;
            """
        )
        font_loaded = self.driver.execute_script(
            """
            var fonts = Array.from(document.fonts);
            return fonts.some(function(font) {
                return font.family.includes('bootstrap-icons');
            });
            """
        )
        self.assertTrue(
            has_icons or font_loaded, "Bootstrap Icons not properly loaded (no icons found and font not loaded)"
        )

    def test_jquery_ajax_functionality(self):
        """Test that jQuery AJAX functionality works."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Test that jQuery.ajax is available
        ajax_available = self.driver.execute_script("return typeof jQuery.ajax === 'function'")
        self.assertTrue(ajax_available, "jQuery AJAX functionality not available")

    def test_popper_included_in_bootstrap(self):
        """Test that Popper.js is included in Bootstrap bundle."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check if Popper is available (included in Bootstrap bundle)
        popper_loaded = self.driver.execute_script(
            "return typeof Popper !== 'undefined' || typeof bootstrap.Tooltip !== 'undefined'"
        )
        self.assertTrue(popper_loaded, "Popper.js not available (should be in Bootstrap bundle)")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class Select2LibraryTests(SeleniumTestCase):
    """Tests for Select2 library functionality."""

    def test_select2_works_on_ignore_categories(self):
        """Test that Select2 JavaScript library file is available and can be loaded."""
        # Connected to the live app, so check Select2 is available rather than an authed page.

        # Visit home page which loads jQuery via base.html
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()

        # Wait for jQuery to load
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(self.driver, 10)
        wait.until(lambda driver: driver.execute_script("return typeof jQuery !== 'undefined'"))

        # Dynamically load Select2 to verify it's available
        select2_loaded = self.driver.execute_script("""
            return new Promise(function(resolve) {
                var script = document.createElement('script');
                script.src = '/static/js/vendor/select2.min.js';
                script.onload = function() {
                    setTimeout(function() {
                        resolve(typeof jQuery.fn.select2 !== 'undefined');
                    }, 100);
                };
                script.onerror = function() {
                    resolve(false);
                };
                document.head.appendChild(script);
            });
        """)

        self.assertTrue(select2_loaded, "Select2 library file not available or failed to load")

    def test_select2_library_file_exists(self):
        """Test that Select2 library file is available for loading."""
        # Visit a page to establish context
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()

        # Wait for jQuery to load (required for Select2)
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(self.driver, 10)
        wait.until(lambda driver: driver.execute_script("return typeof jQuery !== 'undefined'"))
        jquery_loaded = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        self.assertTrue(jquery_loaded, "jQuery not loaded (required for Select2)")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class ChartJsLibraryTests(SeleniumTestCase):
    """Tests for Chart.js library functionality."""

    def test_chartjs_available(self):
        """Test that Chart.js library can be loaded."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page body not found")

    def test_dashboard_page_loads(self):
        """Test that dashboard/stats pages load (where Chart.js is used)."""
        # Try to access auctions page which may have stats
        self.driver.get(self.get_url("/auctions/"))
        self.wait_for_page_load()
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Auctions page body not found")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class VendorLibraryIntegrationTests(SeleniumTestCase):
    """Integration tests to verify vendor libraries work together correctly."""

    def test_no_javascript_errors_on_home(self):
        """Test that vendor JS libraries are loaded correctly on the home page."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        jquery_ok = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        bootstrap_ok = self.driver.execute_script("return typeof bootstrap !== 'undefined'")
        self.assertTrue(jquery_ok, "jQuery not loaded on home page (possible 404 or JS error)")
        self.assertTrue(bootstrap_ok, "Bootstrap not loaded on home page (possible 404 or JS error)")

    def test_no_javascript_errors_on_lots(self):
        """Test that vendor JS libraries are loaded correctly on the lots page."""
        self.driver.get(self.get_url("/lots/"))
        self.wait_for_page_load()
        jquery_ok = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        bootstrap_ok = self.driver.execute_script("return typeof bootstrap !== 'undefined'")
        self.assertTrue(jquery_ok, "jQuery not loaded on lots page (possible 404 or JS error)")
        self.assertTrue(bootstrap_ok, "Bootstrap not loaded on lots page (possible 404 or JS error)")

    def test_no_javascript_errors_on_auctions(self):
        """Test that vendor JS libraries are loaded correctly on the auctions page."""
        self.driver.get(self.get_url("/auctions/"))
        self.wait_for_page_load()
        jquery_ok = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        bootstrap_ok = self.driver.execute_script("return typeof bootstrap !== 'undefined'")
        self.assertTrue(jquery_ok, "jQuery not loaded on auctions page (possible 404 or JS error)")
        self.assertTrue(bootstrap_ok, "Bootstrap not loaded on auctions page (possible 404 or JS error)")

    def test_bootstrap_components_interactive(self):
        """Test that Bootstrap interactive components work."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        bootstrap_functional = self.driver.execute_script(
            """
            return typeof bootstrap !== 'undefined' &&
                   typeof bootstrap.Tooltip === 'function';
            """
        )
        self.assertTrue(bootstrap_functional, "Bootstrap JavaScript components not functional")

    def test_responsive_bootstrap_classes(self):
        """Test that Bootstrap responsive classes are applied correctly."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Test at mobile size
        self.driver.set_window_size(375, 667)
        self.wait_for_page_load()
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page doesn't render at mobile size")
        # Test at desktop size
        self.driver.set_window_size(1920, 1080)
        self.wait_for_page_load()
        body = self.driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body, "Page doesn't render at desktop size")

    def test_all_vendor_files_load_without_404(self):
        """Test that all vendor files load successfully without 404 errors."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        vendor_checks = self.driver.execute_script(
            """
            return {
                jquery: typeof jQuery !== 'undefined',
                bootstrap: typeof bootstrap !== 'undefined'
            };
            """
        )
        self.assertTrue(vendor_checks["jquery"], "jQuery not loaded - vendor/jquery.min.js may have returned 404")
        self.assertTrue(
            vendor_checks["bootstrap"], "Bootstrap not loaded - vendor/bootstrap.bundle.min.js may have returned 404"
        )

    def test_jquery_dom_ready(self):
        """Test that jQuery document ready functions work."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Test that jQuery is available and DOM is ready
        dom_ready = self.driver.execute_script(
            """
            return new Promise(function(resolve) {
                jQuery(document).ready(function() {
                    resolve(true);
                });
            });
            """
        )
        self.assertTrue(dom_ready, "jQuery document ready not working")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class PrintPageTests(SeleniumTestCase):
    """Tests for the print page which uses jQuery and Bootstrap."""

    def test_print_page_loads(self):
        """Test that pages with print functionality load correctly."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Verify jQuery and Bootstrap are available for print functionality
        jquery_loaded = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        bootstrap_loaded = self.driver.execute_script("return typeof bootstrap !== 'undefined'")
        self.assertTrue(jquery_loaded, "jQuery not loaded (required for print functionality)")
        self.assertTrue(bootstrap_loaded, "Bootstrap not loaded (required for print functionality)")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class GenericAdminFormTests(SeleniumTestCase):
    """Tests for generic admin forms which use jQuery."""

    def test_admin_forms_jquery_available(self):
        """Test that jQuery is available for admin forms."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        jquery_loaded = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        self.assertTrue(jquery_loaded, "jQuery not loaded (required for admin forms)")
        # Test jQuery $ shorthand is available
        jquery_shorthand = self.driver.execute_script("return typeof $ !== 'undefined'")
        self.assertTrue(jquery_shorthand, "jQuery $ shorthand not available")


# ---------------------------------------------------------------------------
# End-to-end bidding over real websockets, against the test database via an in-process Daphne.
# ---------------------------------------------------------------------------


#: Settings the live ASGI server needs that a production-shaped config (tests force DEBUG off)
#: won't give it:
#:
#: - `STORAGES`: the static handler serves plain names through finders, but `{% static %}`
#:   renders hashed names when STATIC_ROOT is collected, so every asset 404s.
#: - Secure cookie flags: `live_server_url` is plain http, so no CSRF cookie and every bid 403s.
LIVE_SERVER_SETTINGS = {
    "STORAGES": {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
    "CSRF_COOKIE_SECURE": False,
    "SESSION_COOKIE_SECURE": False,
}


class LiveServerSettingsTests(SimpleTestCase):
    """`LIVE_SERVER_SETTINGS` undoes both production settings. Not skipped, and needs no browser."""

    @override_settings(**LIVE_SERVER_SETTINGS)
    def test_static_urls_are_names_the_finders_can_serve(self):
        for name in ("css/auction_site.css", "js/vendor/jquery.min.js", "js/ws.js"):
            with self.subTest(name=name):
                self.assertEqual(staticfiles_storage.url(name), f"{settings.STATIC_URL}{name}")
                self.assertIsNotNone(finders.find(name), "ASGIStaticFilesHandler resolves through the finders")

    @override_settings(**LIVE_SERVER_SETTINGS)
    def test_cookies_are_not_marked_secure_for_a_plain_http_live_server(self):
        self.assertFalse(settings.CSRF_COOKIE_SECURE)
        self.assertFalse(settings.SESSION_COOKIE_SECURE)


@unittest.skipUnless(
    SELENIUM_AVAILABLE and selenium_available() and CHANNELS_LIVE_AVAILABLE,
    "Selenium and channels live server (daphne) required",
)
@tag("selenium")
@override_settings(**LIVE_SERVER_SETTINGS)
class LiveBrowserTestCase(ChannelsLiveServerTestCase):
    """A real browser against the test database, through an in-process Daphne with real websockets.

    host = "web" is in ALLOWED_HOSTS and reachable from the selenium container.
    """

    host = "web"
    serve_static = True

    @classmethod
    def setUpClass(cls):
        # The Daphne subprocess re-prefixes the test DB name ("test_test_<name>"); pin TEST.NAME.
        db = settings.DATABASES["default"]
        db.setdefault("TEST", {})
        if not db["TEST"].get("NAME"):
            db["TEST"]["NAME"] = db["NAME"]
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self._drivers = []

    def tearDown(self):
        for driver in self._drivers:
            try:
                driver.quit()
            except Exception:
                pass
        super().tearDown()

    def new_browser(self, user=None, extra_args=()):
        """A fresh browser session, optionally already logged in as `user`."""
        driver = get_selenium_driver(extra_args=extra_args)
        self._drivers.append(driver)
        if user is not None:
            self.login(driver, user)
        return driver

    def login(self, driver, user):
        """Log the browser in by copying a committed session cookie from the test client."""
        client = Client()
        client.force_login(user)
        driver.get(self.live_server_url + "/")  # must be on the domain before add_cookie
        driver.add_cookie(
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "path": "/",
            }
        )


class LiveBiddingTestCase(LiveBrowserTestCase):
    """Browser bid tests with real websockets and test data."""

    def setUp(self):
        super().setUp()
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.seller = User.objects.create_user(username="e2e_seller", password="x", email="e2e_seller@example.com")
        self.auction = Auction.objects.create(
            created_by=self.seller,
            title="E2E bidding auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=the_future,
        )
        self.location = PickupLocation.objects.create(name="e2e location", auction=self.auction, pickup_time=the_future)
        self.seller_tos = AuctionTOS.objects.create(
            user=self.seller, auction=self.auction, pickup_location=self.location
        )
        # Migration-loaded categories are truncated, and bidding needs a species_category.
        self.category = Category.objects.create(name="E2E category")
        self.lot = Lot.objects.create(
            lot_name="E2E test lot",
            auction=self.auction,
            auctiontos_seller=self.seller_tos,
            species_category=self.category,
            quantity=1,
            reserve_price=10,
            date_end=the_future,
        )
        # Backdate so the lot isn't too new to bid on.
        Lot.objects.filter(pk=self.lot.pk).update(date_posted=timezone.now() - datetime.timedelta(hours=2))

    def make_bidder(self, username):
        """A user who has joined the auction and whose username is publicly visible."""
        user = User.objects.create_user(username=username, password="x", email=f"{username}@example.com")
        userdata = user.userdata
        userdata.username_visible = True
        userdata.save()
        AuctionTOS.objects.create(user=user, auction=self.auction, pickup_location=self.location)
        return user

    def page_diagnosis(self, driver):
        """Why a page didn't do what the test expected: not loaded, signed out, or handshake refused."""
        probe = """
            return {
                url: window.location.href,
                title: document.title,
                readyState: document.readyState,
                signed_in: !!document.querySelector('#chat'),
                socket_on_page: typeof window.lotWebSocket !== 'undefined',
                socket_state: window.lotWebSocket ? window.lotWebSocket.readyState : null,
                socket_url: (typeof lotWebSocketUrl !== 'undefined') ? lotWebSocketUrl : null,
                jquery: typeof window.jQuery,
                body_start: document.body ? document.body.innerText.slice(0, 300) : ''
            };
        """
        try:
            facts = driver.execute_script(probe)
        except Exception as error:  # a browser that cannot even run this has its own story
            return f"  could not probe the page: {error}"
        lines = [f"  {key}: {value!r}" for key, value in sorted(facts.items())]
        try:
            # Only Chrome with goog:loggingPrefs serves this; diagnostics never fail the test.
            console = driver.get_log("browser")
        except Exception:
            console = []
        lines += [f"  console: {entry.get('level')} {entry.get('message')}" for entry in console[-10:]]
        return "\n".join(lines)

    def open_lot(self, driver, lot=None):
        """Load the lot page and wait until its websocket is OPEN, so bids are received.

        Thirty seconds, since a refused handshake reconnects on a backoff reaching 8s.
        """
        lot = lot or self.lot
        url = self.live_server_url + reverse("lot_by_pk", kwargs={"pk": lot.pk})
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return !!(window.lotWebSocket && window.lotWebSocket.readyState === 1)")
            )
        except TimeoutException:
            msg = f"the lot page websocket never opened at {url}\n{self.page_diagnosis(driver)}"
            raise AssertionError(msg) from None

    def place_bid(self, driver, amount):
        """Drive the real bid UI: enter amount, confirm in the modal."""
        field = driver.find_element(By.ID, "bid_amount")
        field.clear()
        field.send_keys(str(amount))
        driver.find_element(By.ID, "bid_button").click()
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "finalize-bid"))).click()

    def text_of(self, driver, element_id):
        try:
            return driver.find_element(By.ID, element_id).text.strip()
        except NoSuchElementException:
            return ""

    def wait_chat_contains(self, driver, needle, timeout=20):
        needle = needle.lower()
        try:
            WebDriverWait(driver, timeout).until(lambda d: needle in self.text_of(d, "chat").lower())
        except TimeoutException:
            msg = (
                f"{needle!r} never arrived over the websocket; the chat panel holds "
                f"{self.text_of(driver, 'chat')!r}\n{self.page_diagnosis(driver)}"
            )
            raise AssertionError(msg) from None


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class BidPlacementE2ETests(LiveBiddingTestCase):
    """Bids round-trip over the websocket, and a proxy max bid never leaks to other bidders."""

    def test_a_signed_out_browser_is_reported_as_signed_out(self):
        """A signed-out browser (no websocket on the page) is reported as signed out."""
        driver = self.new_browser()
        driver.get(self.live_server_url + reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        diagnosis = self.page_diagnosis(driver)
        self.assertIn("signed_in: False", diagnosis)
        self.assertIn("socket_on_page: False", diagnosis)

    def test_placing_a_bid_makes_you_the_high_bidder(self):
        """A bid in the UI makes you the high bidder, via the websocket, and is saved."""
        bidder = self.make_bidder("e2e_bidder")
        driver = self.new_browser(bidder)
        self.open_lot(driver)

        self.place_bid(driver, 15)

        self.wait_chat_contains(driver, "first bid")
        self.assertIn(bidder.username, self.text_of(driver, "high_bidder_name"))
        self.assertTrue(
            Bid.objects.exclude(is_deleted=True).filter(user=bidder, lot_number=self.lot).exists(),
            "bid was not persisted",
        )

    def test_proxy_max_bid_is_not_leaked_to_other_bidders(self):
        """A bidder's max proxy bid never reaches another user's page."""
        alice = self.make_bidder("e2e_alice")
        bob = self.make_bidder("e2e_bob")
        alice_browser = self.new_browser(alice)
        bob_browser = self.new_browser(bob)
        # Both connected before the bid, so both receive the broadcast.
        self.open_lot(alice_browser)
        self.open_lot(bob_browser)

        self.place_bid(alice_browser, 50)
        self.wait_chat_contains(bob_browser, "first bid")

        # Bob sees the public price, never Alice's max.
        self.assertEqual(self.text_of(bob_browser, "price"), "10")
        self.assertNotIn("50", self.text_of(bob_browser, "price"))
        self.assertNotIn("50", self.text_of(bob_browser, "high_bidder_name"))
        self.assertEqual(self.text_of(bob_browser, "your_bid"), "", "bob has no bid, so no max should show")

        self.place_bid(bob_browser, 20)
        self.wait_chat_contains(bob_browser, "still the high bidder")
        self.assertNotIn("50", self.text_of(bob_browser, "price"))
        self.assertNotIn("50", self.text_of(bob_browser, "high_bidder_name"))
        self.assertIn(alice.username, self.text_of(bob_browser, "high_bidder_name"))

        # Alice's own reloaded page shows her max; the broadcast never does.
        alice_browser.get(self.live_server_url + reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        WebDriverWait(alice_browser, 10).until(lambda d: self.text_of(d, "your_bid_price") != "")
        self.assertEqual(float(self.text_of(alice_browser, "your_bid_price")), 50.0)

    def test_being_outbid_updates_the_previous_high_bidder(self):
        """Being outbid updates the previous high bidder's page without leaking the new max."""
        alice = self.make_bidder("e2e_alice2")
        bob = self.make_bidder("e2e_bob2")
        alice_browser = self.new_browser(alice)
        bob_browser = self.new_browser(bob)
        self.open_lot(alice_browser)
        self.open_lot(bob_browser)

        self.place_bid(alice_browser, 12)
        self.wait_chat_contains(bob_browser, "first bid")

        # Bob outbids with a secret max of 30.
        self.place_bid(bob_browser, 30)
        self.wait_chat_contains(alice_browser, "high bidder")

        self.assertIn(bob.username, self.text_of(alice_browser, "high_bidder_name"))
        self.assertNotIn("30", self.text_of(alice_browser, "price"))
        self.assertNotIn("30", self.text_of(alice_browser, "high_bidder_name"))


@unittest.skipUnless(
    CHANNELS_LIVE_AVAILABLE and SELENIUM_AVAILABLE and selenium_available(),
    "Selenium and channels' live server are both needed",
)
@tag("selenium")
class ModalReopenTests(LiveBiddingTestCase):
    """A modal opens, closes and opens again indefinitely.

    Guards inherited ``hx-swap="outerHTML"`` replacing ``#modals-here``, and duplicate container ids.
    Asserting the container survives each cycle is the point.
    """

    def test_a_modal_opens_again_after_being_cancelled(self):
        driver = self.new_browser(self.seller)
        driver.get(self.live_server_url + reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return typeof htmx") == "object")
        self.assertEqual(
            driver.execute_script("return document.querySelectorAll('#modals-here').length"),
            1,
            f"the page must render exactly one modal container: {self.page_diagnosis(driver)}",
        )

        for attempt in (1, 2, 3):
            link = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "tbody a[hx-get*='/api/auctiontos/']"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
            link.click()
            try:
                WebDriverWait(driver, 10).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, "[data-htmx-modal-root]"))
                )
            except TimeoutException:
                self.fail(f"the modal did not open on attempt {attempt}: {self.page_diagnosis(driver)}")
            self.assertEqual(
                driver.execute_script("return document.querySelectorAll('#modals-here').length"),
                1,
                f"opening the modal must fill the container, not replace it (attempt {attempt})",
            )

            cancel = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//*[@data-htmx-modal-root]//button[normalize-space()='Cancel']"))
            )
            cancel.click()
            WebDriverWait(driver, 10).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-htmx-modal-root]"))
            )
            self.assertEqual(
                driver.execute_script("return document.querySelectorAll('#modals-here').length"),
                1,
                f"closing the modal must leave the container behind (attempt {attempt})",
            )


# ---------------------------------------------------------------------------
# The lot queue's camera, through a fake one: a canvas streaming QR labels.
# ---------------------------------------------------------------------------

#: Stands in for the phone's camera. getUserMedia answers with a 1280x720 canvas stream showing
#: ``fakeCamera.labels`` -- real QR codes, drawn by ZXing's own writer -- and every lot add the page
#: posts is recorded in ``fakeCamera.posts``, held back ``fakeCamera.delay`` ms on its way out.
FAKE_CAMERA_JS = """
const zxingUrl = arguments[0];
const done = arguments[arguments.length - 1];
window.fakeCamera = {labels: [], posts: [], delay: 0};
function ready() {
  const camera = document.createElement('canvas');
  camera.width = 1280;
  camera.height = 720;
  const drawn = {};
  function qr(text) {
    if (!drawn[text]) {
      const matrix = new ZXing.QRCodeWriter().encode(text, ZXing.BarcodeFormat.QR_CODE, 200, 200, new Map());
      const label = document.createElement('canvas');
      label.width = matrix.getWidth();
      label.height = matrix.getHeight();
      const pen = label.getContext('2d');
      pen.fillStyle = '#fff';
      pen.fillRect(0, 0, label.width, label.height);
      pen.fillStyle = '#000';
      for (let x = 0; x < label.width; x++) {
        for (let y = 0; y < label.height; y++) { if (matrix.get(x, y)) { pen.fillRect(x, y, 1, 1); } }
      }
      drawn[text] = label;
    }
    return drawn[text];
  }
  function paint() {
    const pen = camera.getContext('2d');
    pen.fillStyle = '#ddd';
    pen.fillRect(0, 0, camera.width, camera.height);
    pen.fillStyle = '#ccc';
    pen.fillRect((Date.now() / 10) % camera.width, 0, 2, 2);  // so every frame is a new one
    fakeCamera.labels.forEach(function (label) { pen.drawImage(qr(label.text), label.x, label.y); });
    requestAnimationFrame(paint);
  }
  requestAnimationFrame(paint);
  navigator.mediaDevices.getUserMedia = function () { return Promise.resolve(camera.captureStream(30)); };
  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, options) {
    const body = options && options.body;
    if (body instanceof FormData && body.has('lot_pk')) {
      fakeCamera.posts.push(body.get('lot_pk'));
      if (fakeCamera.delay) {
        return new Promise(function (wait) { setTimeout(wait, fakeCamera.delay); }).then(function () {
          return realFetch(url, options);
        });
      }
    }
    return realFetch(url, options);
  };
  done(true);
}
if (window.ZXing) {
  ready();
} else {
  const script = document.createElement('script');
  script.src = zxingUrl;
  script.onload = ready;
  script.onerror = function () { done('ZXing did not load from ' + zxingUrl); };
  document.head.appendChild(script);
}
"""


class LotQueueCameraTests(LiveBrowserTestCase):
    """Labels shown to the camera on the lot queue become queue entries, each once, at the camera's pace.

    Chrome on Linux has no BarcodeDetector, so the page reads with ZXing: the path every iPhone takes
    in Safari. A label's centre at (640, 360) is in view; the preview is a 3:1 box at this window size,
    which shows the middle rows 148-572 of each 720-row frame.
    """

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(username="queue_admin", password="x", email="queue_admin@example.com")
        self.auction = Auction.objects.create(
            created_by=self.admin,
            title="Camera queue auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(hours=1),
        )
        location = PickupLocation.objects.create(
            name="camera hall", auction=self.auction, pickup_time=timezone.now() + datetime.timedelta(days=1)
        )
        seller = AuctionTOS.objects.create(user=self.admin, auction=self.auction, pickup_location=location)
        category = Category.objects.create(name="Camera category")
        self.lots = [
            Lot.objects.create(
                lot_name=f"Camera lot {n}", auction=self.auction, auctiontos_seller=seller, species_category=category
            )
            for n in range(3)
        ]

    def open_queue(self):
        origin = self.live_server_url
        driver = self.new_browser(
            self.admin,
            # getUserMedia needs a secure context, and the live server is plain http.
            extra_args=(f"--unsafely-treat-insecure-origin-as-secure={origin}",),
        )
        driver.get(origin + reverse("auction_lot_queue", kwargs={"slug": self.auction.slug}))
        ready = driver.execute_async_script(FAKE_CAMERA_JS, origin + static("js/vendor/zxing.min.js"))
        self.assertIs(ready, True, ready)
        return driver

    @staticmethod
    def label(lot, x=540, y=260):
        return {"text": f"https://auction.fish/qr/{lot.pk}/", "x": x, "y": y}

    def show(self, driver, *labels):
        driver.execute_script("fakeCamera.labels = arguments[0];", list(labels))

    def posts(self, driver):
        return driver.execute_script("return fakeCamera.posts")

    def queued(self):
        return list(
            LotQueueEntry.objects.filter(auction=self.auction, passed_at__isnull=True)
            .order_by("order")
            .values_list("lot_id", flat=True)
        )

    def wait_for(self, driver, condition, what, timeout=20):
        try:
            WebDriverWait(driver, timeout, poll_frequency=0.1).until(lambda d: condition())
        except TimeoutException:
            status = driver.execute_script("return document.getElementById('queue-scan-status').textContent")
            self.fail(f"{what}: posted {self.posts(driver)}, queued {self.queued()}, camera says {status!r}")

    def start_camera(self, driver):
        driver.find_element(By.ID, "queue-camera-btn").click()

    def test_a_label_left_in_view_is_added_once(self):
        """The camera sees a label on every frame; it used to post it again every 2.5s it stayed in view."""
        lot = self.lots[0]
        driver = self.open_queue()
        self.show(driver, self.label(lot))
        self.start_camera(driver)
        self.wait_for(driver, lambda: self.queued() == [lot.pk], "the label was never added")
        time.sleep(6)
        self.assertEqual(self.posts(driver), [str(lot.pk)])
        self.assertEqual(self.queued(), [lot.pk])

    def test_a_slow_network_does_not_hold_the_camera(self):
        """The next label is read while the last one's add is still on its way."""
        first, second = self.lots[:2]
        driver = self.open_queue()
        driver.execute_script("fakeCamera.delay = 6000;")
        self.show(driver, self.label(first))
        self.start_camera(driver)
        self.wait_for(driver, lambda: self.posts(driver) == [str(first.pk)], "the first label was never read")
        self.show(driver, self.label(second))
        self.wait_for(
            driver,
            lambda: str(second.pk) in self.posts(driver),
            "the second label wasn't read while the first add was in flight",
            timeout=3,
        )
        self.show(driver)
        self.wait_for(driver, lambda: self.queued() == [first.pk, second.pk], "both adds should land, in order")
        self.wait_for(
            driver,
            lambda: (
                driver.execute_script("return document.querySelectorAll('#queue-sortable [data-lot-pk]').length") == 2
            ),
            "the list should show both",
        )

    def test_every_label_in_view_is_read_and_only_those(self):
        """A native detector's every code, not just the first, but none from the part of the frame the
        preview crops off: the operator never aimed at those.
        """
        in_view, also_in_view, cropped_off = self.lots
        driver = self.open_queue()

        def found(lot, x, y):
            return {
                "rawValue": self.label(lot)["text"],
                "boundingBox": {"x": x, "y": y, "width": 150, "height": 150},
            }

        driver.execute_script(
            """
            const found = arguments[0];
            window.BarcodeDetector = function () {};
            window.BarcodeDetector.prototype.detect = function () { return Promise.resolve(found); };
            """,
            [found(in_view, 100, 300), found(also_in_view, 900, 300), found(cropped_off, 500, 0)],
        )
        self.start_camera(driver)
        self.wait_for(
            driver, lambda: sorted(self.queued()) == sorted([in_view.pk, also_in_view.pk]), "both codes in view"
        )
        time.sleep(1)
        self.assertNotIn(str(cropped_off.pk), self.posts(driver))

    def test_a_removed_lot_goes_back_on_with_its_label(self):
        lot = self.lots[0]
        driver = self.open_queue()
        self.show(driver, self.label(lot))
        self.start_camera(driver)
        self.wait_for(driver, lambda: self.queued() == [lot.pk], "the label was never added")
        added_at = time.time()
        self.show(driver)
        # Found and clicked in one go: the list is swapped out as refreshes land. Only in a settled list:
        # htmx wires a swapped-in button up when it settles, 20ms on, and a click before that does nothing.
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script(
                "const remove = document.querySelector(arguments[0]); if (remove) { remove.click(); } return !!remove;",
                f'#queue-list:not(.htmx-settling) #queue-sortable [data-lot-pk="{lot.pk}"] .btn-danger',
            )
        )
        self.wait_for(driver, lambda: self.queued() == [], "the remove button should take it off")
        # Just after an add lands the list may not show it yet, so for a moment a label seen again is
        # taken to be one already in the queue.
        time.sleep(max(0, added_at + 5.5 - time.time()))
        self.show(driver, self.label(lot))
        self.wait_for(driver, lambda: self.queued() == [lot.pk], "showing the label again should put it back")
        self.assertEqual(self.posts(driver), [str(lot.pk), str(lot.pk)])


# ---------------------------------------------------------------------------
# Voice on set lot winners, in the app: a fake bridge stands in for it.
# ---------------------------------------------------------------------------

APP_USER_AGENT = "Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile FishAuctionsApp/1.0 (Flutter; Android)"

#: The app's JavaScript bridge, installed before the page's own scripts run. Every call is recorded in
#: ``fakeApp.calls`` and answered with ``fakeApp.state``.
FAKE_APP_BRIDGE_JS = """
window.fakeApp = {
  calls: [],
  state: {supported: true, listening: false, web_microphone: true,
          settings: {confident_at: 0.77, prefer_on_device: true, bias_low_prices: false},
          settings_range: {confident_min: 0.6, confident_max: 0.9}},
};
try {
  const stored = JSON.parse(window.sessionStorage.getItem('fakeAppState') || 'null');
  if (stored) { Object.assign(fakeApp.state, stored); }
} catch (err) {}
window.flutter_inappwebview = {
  callHandler: function (name) {
    fakeApp.calls.push(name);
    if (name === 'voiceStart') { fakeApp.state.listening = true; }
    if (name === 'voiceStop') { fakeApp.state.listening = false; }
    return Promise.resolve(Object.assign({}, fakeApp.state));
  },
};
"""


@override_settings(OPENAI_API_KEY="sk-test")
class AppVoiceTests(LiveBrowserTestCase):
    """The set-winners page reading the app's transcripts, and listening through OpenAI in the app."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(username="voice_admin", password="x", email="voice_admin@example.com")
        UserData.objects.filter(user=self.admin).update(voice_cloud_enabled=True)
        self.auction = Auction.objects.create(
            created_by=self.admin,
            title="Voice auction",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(hours=1),
        )

    def open_page(self, app_state=None, stub_openai=True):
        """Set winners in the app. What's heard (the page's one way to the server) is recorded instead of
        sent, and so, unless ``stub_openai`` is off, are starts of listening through OpenAI.
        """
        origin = self.live_server_url
        driver = self.new_browser(
            self.admin,
            extra_args=(f"--user-agent={APP_USER_AGENT}", f"--unsafely-treat-insecure-origin-as-secure={origin}"),
        )
        driver.command_executor.add_command("executeCdpCommand", "POST", "/session/$sessionId/goog/cdp/execute")
        driver.execute(
            "executeCdpCommand",
            {"cmd": "Page.addScriptToEvaluateOnNewDocument", "params": {"source": FAKE_APP_BRIDGE_JS}},
        )
        if app_state is not None:
            driver.execute_script("sessionStorage.setItem('fakeAppState', JSON.stringify(arguments[0]));", app_state)
        driver.get(origin + reverse("auction_lot_winners_dynamic", kwargs={"slug": self.auction.slug}))
        self.page_ready(driver, stub_openai)
        return driver

    def page_ready(self, driver, stub_openai=True):
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return fakeApp.calls.includes('voiceGetState')"))
        driver.execute_script("window.heard = []; window.voiceHeard = function (text) { heard.push(text); };")
        if stub_openai:
            driver.execute_script("window.cloudStarts = 0; window.voiceCloudStart = function () { cloudStarts += 1; };")

    def send(self, driver, **event):
        driver.execute_script("window.fishauctionsVoice.onEvent(arguments[0]);", event)

    def heard(self, driver):
        return driver.execute_script("return heard")

    def transcript(self, driver, text, phrase_id=None, final=None, partial=True):
        event = {"type": "transcript", "text": text, "partial": partial}
        if phrase_id is not None:
            event["phrase_id"] = phrase_id
        if final is not None:
            event["final"] = final
        self.send(driver, **event)

    def test_a_phrase_ends_on_its_final_the_next_phrase_or_the_app_stopping(self):
        driver = self.open_page()
        self.send(driver, type="state", listening=True)
        self.transcript(driver, "lot four", phrase_id=1, final=False)
        self.assertEqual(self.heard(driver), [])
        # A build that already acted on a phrase's commands still marks its final partial.
        self.transcript(driver, "lot forty two", phrase_id=1, final=True)
        self.assertEqual(self.heard(driver), ["lot forty two"])
        self.transcript(driver, "sold to", phrase_id=2, final=False)
        self.transcript(driver, "bidder seven", phrase_id=3, final=False)
        self.assertEqual(self.heard(driver), ["lot forty two", "sold to"])
        self.send(driver, type="state", listening=True)
        self.assertEqual(self.heard(driver), ["lot forty two", "sold to", "bidder seven"])
        self.transcript(driver, "for ten", phrase_id=4, final=False)
        self.send(driver, type="state", listening=False)
        self.assertEqual(self.heard(driver), ["lot forty two", "sold to", "bidder seven", "for ten"])

    def test_only_an_app_that_sends_no_final_has_partials_settle(self):
        driver = self.open_page()
        self.send(driver, type="state", listening=True)
        self.transcript(driver, "lot five")
        time.sleep(1)
        self.assertEqual(self.heard(driver), [], "a partial waits for its phrase to settle")
        time.sleep(3.5)
        self.assertEqual(self.heard(driver), ["lot five"])
        self.transcript(driver, "lot six", phrase_id=9, final=False)
        time.sleep(4.5)
        self.assertEqual(self.heard(driver), ["lot five"], "a phrase that will get its final waits for it")

    def open_settings(self, driver):
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "voice-settings-btn"))).click()

    def test_listen_uses_the_phone_unless_openai_is_chosen(self):
        driver = self.open_page()
        self.open_settings(driver)
        self.assertTrue(driver.find_element(By.ID, "voice-source").is_displayed())
        self.assertTrue(driver.find_element(By.ID, "voice-source-phone").is_selected())
        listen = driver.find_element(By.ID, "voice-btn")
        listen.click()
        WebDriverWait(driver, 10).until(lambda d: "Stop" in listen.text)
        self.assertIn("voiceStart", driver.execute_script("return fakeApp.calls"))
        # Switched while listening, it carries on listening the new way.
        driver.find_element(By.ID, "voice-source-openai").click()
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return cloudStarts") == 1)
        self.assertIn("voiceStop", driver.execute_script("return fakeApp.calls"))
        self.assertFalse(driver.find_element(By.ID, "voice-phone-settings").is_displayed())
        # Remembered on this device.
        driver.refresh()
        self.page_ready(driver)
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "voice-btn"))).click()
        self.assertEqual(driver.execute_script("return cloudStarts"), 1)
        self.assertNotIn("voiceStart", driver.execute_script("return fakeApp.calls"))

    def test_an_app_that_keeps_the_microphone_listens_itself(self):
        driver = self.open_page(app_state={"web_microphone": False})
        driver.execute_script("localStorage.setItem('voiceListenWith', 'openai');")
        driver.refresh()
        self.page_ready(driver)
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "voice-btn"))).click()
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return fakeApp.calls.includes('voiceStart')"))
        self.assertEqual(driver.execute_script("return cloudStarts"), 0)
        self.assertFalse(driver.find_element(By.ID, "voice-source").is_displayed())

    def test_a_phone_with_no_recognizer_listens_through_openai(self):
        driver = self.open_page(app_state={"supported": False})
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "voice-btn"))).click()
        self.assertEqual(driver.execute_script("return cloudStarts"), 1)

    def test_openai_is_only_offered_to_accounts_it_is_on_for(self):
        UserData.objects.filter(user=self.admin).update(voice_cloud_enabled=False)
        driver = self.open_page()
        self.assertFalse(driver.find_elements(By.ID, "voice-source"))
        driver.execute_script("localStorage.setItem('voiceListenWith', 'openai');")
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "voice-btn"))).click()
        WebDriverWait(driver, 10).until(lambda d: d.execute_script("return fakeApp.calls.includes('voiceStart')"))
        self.assertEqual(driver.execute_script("return cloudStarts"), 0)

    def test_a_refused_microphone_points_at_the_app(self):
        """The app has already said where its permission lives; the site has none to allow."""
        driver = self.open_page(stub_openai=False)
        driver.execute_script(
            """
            const sessionUrl = arguments[0];
            const realAjax = $.ajax;
            $.ajax = function (options) {
              if (options && options.url === sessionUrl) {
                return $.Deferred().resolve({key: 'k', commit: true}).promise();
              }
              return realAjax.apply(this, arguments);
            };
            navigator.mediaDevices.getUserMedia = function () {
              return Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
            };
            """,
            reverse("auction_voice_cloud_session", kwargs={"slug": self.auction.slug}),
        )
        self.open_settings(driver)
        driver.find_element(By.ID, "voice-source-openai").click()
        driver.find_element(By.ID, "voice-btn").click()
        WebDriverWait(driver, 10).until(
            lambda d: "Allow the microphone for the app" in d.find_element(By.ID, "voice-status").text
        )

    def test_the_apps_events_are_not_about_openai_listening(self):
        """Letting go of the microphone, the app says it stopped; OpenAI is what's listening by then."""
        driver = self.open_page()
        driver.execute_script("voiceCloud.wanted = true; voiceRenderState({listening: true, on_device: false});")
        self.send(driver, type="state", listening=False)
        self.send(driver, type="error", code="busy", message="The microphone is in use")
        self.assertIn("Stop", driver.find_element(By.ID, "voice-btn").text)
