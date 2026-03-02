import asyncio
import logging
import random
from playwright.async_api import async_playwright
import config


# Realistic, recent User-Agent strings to rotate through
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# Viewport sizes that look like real monitors
_VIEWPORTS = [
    {'width': 1920, 'height': 1080},
    {'width': 1536, 'height': 864},
    {'width': 1440, 'height': 900},
    {'width': 1366, 'height': 768},
    {'width': 1280, 'height': 800},
]

# Patterns that indicate a block/error page rather than real content
BLOCK_PAGE_PATTERNS = [
    # Bot protection / access denied
    'there was a problem providing the content you requested',
    'please contact our support team',
    'access denied',
    'you have been blocked',
    'this request was blocked',
    'sorry, you have been blocked',
    'why have i been blocked',
    'automated access to this page',
    'bot or crawler',
    'please verify you are a human',
    'enable javascript and cookies to continue',
    'unusual traffic from your computer',
    'reference number:',
    'your ip has been',
    'too many requests',
    'rate limit exceeded',
    'we noticed unusual activity',
    'are you a robot',
    'verify you are human',
    'one more step',
    'pardon our interruption',
    'we need to verify',
    'before you continue',
    'attention required',
    'security check',
    # CAPTCHA / challenge
    'captcha',
    'hcaptcha',
    'recaptcha',
    # Cookie consent / GDPR banners (page didn't load real content)
    'manage consent preferences',
    'cookie preferences',
    'types of cookies',
    'we respect your right to privacy',
    'this site uses cookies',
    'we use cookies',
    'cookie consent',
    'consent preferences',
    'strictly necessary cookies',
    'performance cookies',
    'functional cookies',
    'targeting cookies',
    'accept all cookies',
    'reject all cookies',
    'manage cookies',
    'cookie policy',
    'privacy preferences',
    # Network / proxy blocks (Jina, Cloudflare workers, etc.)
    'blocked by network security',
    'this page is blocked',
    'network security policy',
    'web filter',
    # Login walls (got login page instead of content)
    'log in to your reddit account',
    'sign in to continue',
    'create an account',
    'you must be logged in',
    'please log in',
    'login required',
    # Paywall indicators
    'subscribe to continue reading',
    'this content is for subscribers',
    'you have reached your limit',
    'article limit reached',
]


def is_block_page(text):
    """Check if extracted text looks like a bot-block / error / consent page.

    Used by both playwright_scraper.py and scraper.py to reject garbage content.
    Returns True if content is garbage that should NOT be saved.
    """
    if not text:
        return True
    text_lower = text.lower().strip()

    # Very short "content" with any single block indicator is garbage
    if len(text_lower) < 200:
        for pattern in BLOCK_PAGE_PATTERNS:
            if pattern in text_lower:
                return True

    # For medium-length text (200-1500 chars), check entire text — these are
    # often cookie consent pages or error pages padded with boilerplate
    if len(text_lower) < 1500:
        matches = sum(1 for p in BLOCK_PAGE_PATTERNS if p in text_lower)
        if matches >= 2:
            return True

    # For longer text, check the first 800 chars (block pages sometimes
    # have boilerplate footer text that inflates length)
    head = text_lower[:800]
    matches = sum(1 for p in BLOCK_PAGE_PATTERNS if p in head)
    if matches >= 2:
        return True

    return False


class PlaywrightBrowserPool:
    """Manages a singleton Playwright browser instance with page concurrency limits."""

    def __init__(self, max_pages=None):
        self._playwright = None
        self._browser = None
        self._semaphore = asyncio.Semaphore(max_pages or config.PLAYWRIGHT_MAX_PAGES)
        self._lock = asyncio.Lock()
        self._launched = False

    async def _ensure_browser(self):
        """Lazily launch Chromium on first use. Auto-recovers from browser crashes."""
        if self._launched and self._browser and self._browser.is_connected():
            return

        async with self._lock:
            if self._launched and self._browser and self._browser.is_connected():
                return

            # Clean up stale instance if browser crashed
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
                self._browser = None

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    # NOTE: --single-process REMOVED — it causes browser crashes
                    # when multiple tabs are opened concurrently
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-default-apps',
                    '--no-first-run',
                    # Anti-detection flags
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                ]
            )
            self._launched = True
            logging.info("Playwright: Chromium browser launched successfully.")

    async def get_page(self):
        """Acquire a semaphore slot and return a new stealth-configured page.

        Retries browser launch once if the browser crashed between _ensure and new_context.
        """
        await self._ensure_browser()
        await self._semaphore.acquire()
        try:
            ua = random.choice(_USER_AGENTS)
            viewport = random.choice(_VIEWPORTS)

            # Try to create context; if browser crashed, re-launch once
            for attempt in range(2):
                try:
                    context = await self._browser.new_context(
                        viewport=viewport,
                        user_agent=ua,
                        locale='en-US',
                        timezone_id='America/New_York',
                        java_script_enabled=True,
                        color_scheme='light',
                        has_touch=False,
                        is_mobile=False,
                    )
                    break  # Success
                except Exception as e:
                    if attempt == 0:
                        logging.warning(f"Playwright: Browser context failed ({str(e)[:80]}), re-launching...")
                        self._launched = False
                        await self._ensure_browser()
                    else:
                        raise

            page = await context.new_page()

            # Apply stealth patches (playwright-stealth library)
            try:
                from playwright_stealth import Stealth
                stealth = Stealth()
                await stealth.apply_stealth_async(page)
            except ImportError:
                pass  # Manual stealth below covers the basics
            except Exception as e:
                logging.debug(f"Stealth patches failed: {e}")

            # Manual stealth JS — always runs for extra coverage
            await page.add_init_script("""
                // Remove webdriver flag
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

                // Fake plugins array (headless has 0 plugins)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });

                // Fake languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });

                // Override permissions query for notifications
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );

                // Fake Chrome runtime
                window.chrome = {
                    runtime: {},
                    loadTimes: function() { return {}; },
                    csi: function() { return {}; },
                    app: { isInstalled: false },
                };

                // Spoof hardware concurrency
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

                // Spoof deviceMemory
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

                // Remove automation-related properties
                delete navigator.__proto__.webdriver;
            """)

            return page, context
        except Exception:
            self._semaphore.release()
            raise

    async def release_page(self, page, context):
        """Close a page/context and release the semaphore slot."""
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        try:
            if context:
                await context.close()
        except Exception:
            pass
        finally:
            self._semaphore.release()

    async def shutdown(self):
        """Shut down the browser and Playwright instance."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._launched = False
        self._browser = None
        self._playwright = None
        logging.info("Playwright: Browser shut down.")


async def scrape_with_playwright(pool, url, timeout=None):
    """Scrape a URL using a real headless Chromium browser via Playwright.

    Handles Cloudflare JS challenges by waiting for them to resolve.
    Detects block/error pages and returns failure instead of garbage content.

    Returns:
        tuple: (html_content, error_string) - html_content is None on failure
    """
    timeout = timeout or config.PLAYWRIGHT_TIMEOUT
    page = None
    context = None

    try:
        page, context = await pool.get_page()

        # Block heavy resources to save memory and speed up loading
        await context.route("**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,eot}",
                           lambda route: route.abort())

        # Small random delay to look more human
        await asyncio.sleep(random.uniform(0.5, 2.0))

        # First attempt: wait for network to go idle
        response = None
        try:
            response = await page.goto(url, wait_until='networkidle', timeout=timeout)
        except Exception:
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
                await page.wait_for_timeout(3000)
            except Exception as nav_err:
                return None, f"Playwright: Navigation failed - {str(nav_err)[:100]}"

        if response is None:
            return None, "Playwright: No response received"

        status = response.status
        if status >= 400 and status != 403:
            return None, f"Playwright: HTTP {status}"

        content = await page.content()

        # Detect Cloudflare/bot challenge pages and wait for resolution
        challenge_indicators = [
            'cf-challenge-running',
            'challenge-platform',
            'just a moment',
            'checking your browser',
            'checking if the site connection is secure',
            'ray id',
            'cf-turnstile',
        ]
        content_lower = content.lower()
        if any(indicator in content_lower for indicator in challenge_indicators):
            logging.info(f"Playwright: Challenge page detected for {url}, waiting for resolution...")
            try:
                await page.wait_for_function(
                    """() => {
                        const body = document.body ? document.body.innerText : '';
                        return !body.includes('Just a moment') &&
                               !body.includes('Checking your browser') &&
                               !document.querySelector('#cf-challenge-running') &&
                               !document.querySelector('.cf-turnstile');
                    }""",
                    timeout=20000
                )
                await page.wait_for_timeout(2000)
                content = await page.content()
            except Exception:
                # Secondary strategy: try clicking any visible accept/continue button
                try:
                    for selector in [
                        'button:has-text("Accept")', 'button:has-text("Continue")',
                        'button:has-text("I agree")', 'button:has-text("OK")',
                        'input[type="submit"]', '#challenge-form button',
                    ]:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=1000):
                            await btn.click()
                            await page.wait_for_timeout(3000)
                            break
                except Exception:
                    pass
                logging.warning(f"Playwright: Challenge did not resolve for {url}")
                content = await page.content()

        if len(content) < 500:
            return None, "Playwright: Page content too short (likely error/challenge page)"

        return content, None

    except Exception as e:
        err_str = str(e)
        # TargetClosedError means browser was shut down while this page was loading
        # This is expected during batch shutdown — return clean error, don't log as crash
        if 'Target' in err_str and 'closed' in err_str:
            return None, f"Playwright: Browser was shut down (batch complete)"
        return None, f"Playwright error: {err_str[:150]}"
    finally:
        if page and context:
            try:
                await pool.release_page(page, context)
            except Exception:
                pass  # Browser may already be closed
