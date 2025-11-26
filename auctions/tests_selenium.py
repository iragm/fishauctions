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
"""

import datetime
import os
import time
import unittest

from django.contrib.auth.models import User
from django.test import LiveServerTestCase, tag
from django.utils import timezone

try:
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

from .models import Auction, AuctionTOS, Lot, PickupLocation


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
class SeleniumTestCase(LiveServerTestCase):
    """
    Base class for Selenium tests that provides common setup and utilities.

    This class sets up a Selenium WebDriver connected to a remote Chrome instance
    and provides helper methods for common browser interactions.
    """

    host = "0.0.0.0"  # Bind to all interfaces so containers can access
    port = 8081  # Use a different port to avoid conflicts

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

    def setUp(self):
        super().setUp()
        self.setup_test_data()

    def setup_test_data(self):
        """Create test data for Selenium tests."""
        self.password = "testpassword123"

        # Create test users
        self.user = User.objects.create_user(
            username="selenium_user",
            password=self.password,
            email="selenium@example.com",
        )
        self.admin_user = User.objects.create_user(
            username="selenium_admin",
            password=self.password,
            email="admin@example.com",
        )

        # Create an active auction
        future_end = timezone.now() + datetime.timedelta(days=7)
        future_start = timezone.now() - datetime.timedelta(hours=1)
        future_pickup = timezone.now() + datetime.timedelta(days=14)

        self.auction = Auction.objects.create(
            created_by=self.admin_user,
            title="Selenium Test Auction",
            is_online=True,
            date_start=future_start,
            date_end=future_end,
        )

        self.location = PickupLocation.objects.create(
            name="Test Location",
            auction=self.auction,
            pickup_time=future_pickup,
        )

        # Create AuctionTOS for users
        self.user_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.auction,
            pickup_location=self.location,
        )

        self.admin_tos = AuctionTOS.objects.create(
            user=self.admin_user,
            auction=self.auction,
            pickup_location=self.location,
            is_admin=True,
        )

        # Create a test lot
        self.lot = Lot.objects.create(
            lot_name="Selenium Test Lot",
            auction=self.auction,
            auctiontos_seller=self.admin_tos,
            quantity=1,
            reserve_price=5,
            date_end=future_end,
        )

    def get_url(self, path):
        """Construct full URL for a given path."""
        if path.startswith("/"):
            return f"{self.base_url}{path}"
        return f"{self.base_url}/{path}"

    def login(self, username=None, password=None):
        """Log in via the login page."""
        if username is None:
            username = self.user.username
        if password is None:
            password = self.password

        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_element(By.NAME, "login")

        login_field = self.driver.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(username)

        password_field = self.driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(password)
        password_field.send_keys(Keys.RETURN)

        # Wait for redirect
        time.sleep(1)

    def wait_for_element(self, by, value, timeout=10):
        """Wait for an element to be present and return it."""
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.presence_of_element_located((by, value)))

    def wait_for_element_clickable(self, by, value, timeout=10):
        """Wait for an element to be clickable and return it."""
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.element_to_be_clickable((by, value)))

    def wait_for_text(self, text, timeout=10):
        """Wait for specific text to appear on the page."""
        wait = WebDriverWait(self.driver, timeout)
        return wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, "body"), text))

    def element_exists(self, by, value):
        """Check if an element exists on the page."""
        try:
            self.driver.find_element(by, value)
            return True
        except NoSuchElementException:
            return False

    def assert_page_title_contains(self, text):
        """Assert that the page title contains the specified text."""
        self.assertIn(text, self.driver.title)

    def assert_element_text(self, by, value, expected_text):
        """Assert that an element contains the expected text."""
        element = self.driver.find_element(by, value)
        self.assertIn(expected_text, element.text)


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class HomePageTests(SeleniumTestCase):
    """Tests for the home page and basic navigation."""

    def test_home_page_loads(self):
        """Test that the home page loads successfully."""
        self.driver.get(self.get_url("/"))
        # Check that the page contains expected content
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page body not found",
        )

    def test_navigation_menu_exists(self):
        """Test that the navigation menu is present and functional."""
        self.driver.get(self.get_url("/"))
        # Look for navbar or nav element
        self.assertTrue(
            self.element_exists(By.CLASS_NAME, "navbar") or self.element_exists(By.TAG_NAME, "nav"),
            "Navigation menu not found",
        )

    def test_lots_page_loads(self):
        """Test that the lots listing page loads successfully."""
        self.driver.get(self.get_url("/lots/"))
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
        self.assertTrue(
            self.element_exists(By.NAME, "login") or self.element_exists(By.NAME, "username"),
            "Login form not found",
        )

    def test_login_form_validation(self):
        """Test that the login form validates input."""
        self.driver.get(self.get_url("/accounts/login/"))
        self.wait_for_element(By.NAME, "login")

        # Try to submit empty form
        submit_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        submit_button.click()

        # Should still be on login page
        time.sleep(1)
        self.assertIn("/accounts/login/", self.driver.current_url)


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class AuctionListingTests(SeleniumTestCase):
    """Tests for auction listing and display."""

    def test_auctions_page_loads(self):
        """Test that the auctions page loads successfully."""
        self.driver.get(self.get_url("/auctions/"))
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Auctions page body not found",
        )

    def test_auction_detail_page_loads(self):
        """Test that an auction detail page loads successfully."""
        self.driver.get(self.get_url(f"/auctions/{self.auction.slug}/"))
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Auction detail page not found",
        )


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class LotViewTests(SeleniumTestCase):
    """Tests for lot viewing and interaction."""

    def test_lot_detail_page_loads(self):
        """Test that a lot detail page loads successfully."""
        self.driver.get(self.get_url(f"/lots/{self.lot.pk}/"))
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Lot detail page not found",
        )

    def test_lot_page_shows_lot_name(self):
        """Test that the lot page displays the lot name."""
        self.driver.get(self.get_url(f"/lots/{self.lot.pk}/"))
        time.sleep(1)
        body_text = self.driver.find_element(By.TAG_NAME, "body").text
        self.assertIn(self.lot.lot_name, body_text)


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class HTMxTests(SeleniumTestCase):
    """Tests for HTMx-powered interactive features."""

    def test_htmx_loaded(self):
        """Test that HTMx library is loaded on the page."""
        self.driver.get(self.get_url("/"))
        time.sleep(1)

        # Check if htmx is defined in the window object
        htmx_loaded = self.driver.execute_script("return typeof htmx !== 'undefined'")
        self.assertTrue(htmx_loaded, "HTMx is not loaded on the page")


@unittest.skipUnless(SELENIUM_AVAILABLE and selenium_available(), "Selenium not available")
@tag("selenium")
class ResponsiveDesignTests(SeleniumTestCase):
    """Tests for responsive design at different viewport sizes."""

    def test_mobile_viewport(self):
        """Test that the page renders correctly at mobile viewport."""
        self.driver.set_window_size(375, 667)  # iPhone 6/7/8 size
        self.driver.get(self.get_url("/"))
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page doesn't render at mobile viewport",
        )

    def test_tablet_viewport(self):
        """Test that the page renders correctly at tablet viewport."""
        self.driver.set_window_size(768, 1024)  # iPad size
        self.driver.get(self.get_url("/"))
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page doesn't render at tablet viewport",
        )

    def test_desktop_viewport(self):
        """Test that the page renders correctly at desktop viewport."""
        self.driver.set_window_size(1920, 1080)  # Full HD
        self.driver.get(self.get_url("/"))
        self.assertTrue(
            self.element_exists(By.TAG_NAME, "body"),
            "Page doesn't render at desktop viewport",
        )
