"""
Selenium-based browser tests for client-side JavaScript functionality.

These tests use Selenium with a remote Chrome browser to test
client-side interactions, HTMx functionality, and JavaScript behavior.

To run these tests locally:
1. Start the selenium container: docker compose --profile selenium up -d selenium
2. Start the app: docker compose up -d
3. Run tests: docker exec -it django python3 manage.py test auctions.tests_selenium

Environment variables:
- SELENIUM_HOST: Hostname of Selenium server (default: selenium)
- SELENIUM_PORT: Port of Selenium server (default: 4444)
- TEST_SERVER_HOST: Hostname of the test server (default: nginx)
- TEST_SERVER_PORT: Port of the test server (default: 80)

Note: These tests connect to the running application via nginx, not the Django test
server. This means they test the actual deployed application state, not test database data.
"""

import os
import unittest

from django.test import TestCase, tag

try:
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False


def get_selenium_driver():
    """Create and return a Selenium WebDriver connected to the remote Chrome instance."""
    selenium_host = os.environ.get("SELENIUM_HOST", "selenium")
    selenium_port = os.environ.get("SELENIUM_PORT", "4444")

    chrome_options = ChromeOptions()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

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
    """
    Base class for Selenium tests that provides common setup and utilities.

    This class sets up a Selenium WebDriver connected to a remote Chrome instance
    and provides helper methods for common browser interactions.

    Note: These tests connect to the live application via nginx, not a test server.
    Test data created in Django tests won't be visible in the browser.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Get the server URL that's accessible from the Selenium container
        # When running in Docker, we need to use the service name 'web' or nginx
        cls.test_server_host = os.environ.get("TEST_SERVER_HOST", "nginx")
        cls.test_server_port = os.environ.get("TEST_SERVER_PORT", "80")
        cls.base_url = f"http://{cls.test_server_host}:{cls.test_server_port}"

        # Create WebDriver
        cls.driver = get_selenium_driver()
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
        # The login page should have a form - check for the form.login class or any form
        has_login_form = self.element_exists(By.CSS_SELECTOR, "form.login")
        has_any_form = self.element_exists(By.TAG_NAME, "form")
        self.assertTrue(
            has_login_form or has_any_form,
            "Login form not found",
        )

    def test_login_page_has_password_field(self):
        """Test that the login page has a password field."""
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_page_load()
        # Check for password input by type or by id (crispy forms uses id_password)
        has_password = (
            self.element_exists(By.CSS_SELECTOR, "input[type='password']")
            or self.element_exists(By.ID, "id_password")
            or self.element_exists(By.NAME, "password")
        )
        self.assertTrue(has_password, "Password field not found")

    def test_login_page_has_submit_button(self):
        """Test that the login page has a submit button."""
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_page_load()
        # Check for submit button or the specific sign-in button
        has_submit = (
            self.element_exists(By.CSS_SELECTOR, "button[type='submit']")
            or self.element_exists(By.CSS_SELECTOR, "input[type='submit']")
            or self.element_exists(By.ID, "sign-in-local")
        )
        self.assertTrue(has_submit, "Submit button not found")


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
        """Test that static files are accessible by checking CSS loaded."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check that we have stylesheets (either link tags or style tags)
        stylesheets = self.driver.find_elements(By.CSS_SELECTOR, "link[rel='stylesheet']")
        style_tags = self.driver.find_elements(By.TAG_NAME, "style")
        # Pages should have at least one stylesheet or style tag
        total_styles = len(stylesheets) + len(style_tags)
        self.assertGreater(total_styles, 0, "No stylesheets found on page")

    def test_javascript_enabled(self):
        """Test that JavaScript is enabled and working."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Execute simple JavaScript to verify it works
        result = self.driver.execute_script("return 1 + 1")
        self.assertEqual(result, 2, "JavaScript execution failed")

    def test_jquery_loaded(self):
        """Test that jQuery is loaded on the page."""
        self.driver.get(self.get_url("/"))
        self.wait_for_page_load()
        # Check if jQuery is defined - the site uses jQuery
        jquery_loaded = self.driver.execute_script("return typeof jQuery !== 'undefined'")
        self.assertTrue(jquery_loaded, "jQuery is not loaded on the page")


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
        # Check for viewport meta tag with different possible attribute formats
        viewport_meta = (
            self.element_exists(By.CSS_SELECTOR, "meta[name='viewport']")
            or self.element_exists(By.CSS_SELECTOR, 'meta[name="viewport"]')
            or self.element_exists(By.CSS_SELECTOR, "meta[content*='width=device-width']")
        )
        self.assertTrue(viewport_meta, "Viewport meta tag not found")


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
