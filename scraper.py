import os.path
import re
import asyncio
import aiohttp
import random
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from curl_cffi.requests import AsyncSession
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
import trafilatura
from bs4 import BeautifulSoup
import logging
import io
from pypdf import PdfReader
# Import config
import config
from playwright_scraper import PlaywrightBrowserPool, scrape_with_playwright, is_block_page

# If modifying these scopes, delete the file token.json.
SCOPES = [
    'https://www.googleapis.com/auth/documents.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.SCRAPER_LOG_FILE),
        logging.StreamHandler()
    ]
)

# Suppress noisy third-party loggers
logging.getLogger('trafilatura').setLevel(logging.ERROR)
logging.getLogger('htmldate').setLevel(logging.ERROR)

class DocScraper:
    def __init__(self):
        self.creds = self._authenticate()
        self.output_file = "raw_scraped_content.md"
        self.failed_log = "failed_links.log"
        self.browser_pool = None  # Set by main_service.py per batch

    def _authenticate(self):
        creds = None
        
        # 1. Try to load from Environment Variables (Cloud/Render)
        print(f"DEBUG: Available Env Vars: {list(os.environ.keys())}")
        env_token = os.environ.get("GOOGLE_TOKEN_JSON")
        env_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        print(f"DEBUG: GOOGLE_TOKEN_JSON present: {bool(env_token)}, Length: {len(env_token) if env_token else 0}")
        print(f"DEBUG: GOOGLE_CREDENTIALS_JSON present: {bool(env_creds)}")

        if env_token:
            import json
            try:
                # Load directly from JSON string in Env Var
                info = json.loads(env_token)
                creds = Credentials.from_authorized_user_info(info, SCOPES)
                logging.info("Authenticated using GOOGLE_TOKEN_JSON environment variable.")
            except Exception as e:
                print(f"DEBUG: JSON Load Error: {e}")
                logging.error(f"Failed to load token from Env Var: {e}")

        # 2. Try to load from Local File (if Env Var didn't work or wasn't present)
        if not creds and os.path.exists(config.TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(config.TOKEN_FILE, SCOPES)
            
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Failed to refresh token: {e}")
                    creds = None
            
            if not creds:
                # If we have env vars for credentials but no token yet (unlikely in cloud, but possible)
                if env_creds:
                     raise PermissionError("Initial authentication must be done locally to generate a token. Please run locally first.")

                if not os.path.exists(config.CREDENTIALS_FILE):
                     raise FileNotFoundError(f"{config.CREDENTIALS_FILE} not found and no Env Vars provided.")
                
                # Check if we are in a headless/automated environment
                import sys
                if not sys.stdin.isatty():
                    raise PermissionError("Authentication required, but no valid token found in non-interactive session.")
                
                flow = InstalledAppFlow.from_client_secrets_file(config.CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            
            # Save the refreshed/new token LOCALLY only (don't try to write to env vars)
            if not env_token:
                with open(config.TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
        return creds

    def get_doc_content(self, document_id):
        try:
            service = build('docs', 'v1', credentials=self.creds)
            # includeTabsContent is required to retrieve the tabs structure
            document = service.documents().get(documentId=document_id, includeTabsContent=True).execute()
            return document
        except HttpError as err:
            logging.error(f"An error occurred fetching document: {err}")
            return None


    def _find_links_in_element(self, element):
        links = []
        
        # Check for paragraph elements
        if 'paragraph' in element:
            for inner_element in element.get('paragraph').get('elements'):
                links.extend(self._extract_from_text_run(inner_element))
        
        # Check for table elements
        elif 'table' in element:
            for row in element.get('table').get('tableRows'):
                for cell in row.get('tableCells'):
                    for cell_element in cell.get('content'):
                        links.extend(self._find_links_in_element(cell_element))
        
        # Check for list elements (handled via paragraph usually, but good to be safe)
        elif 'tableOfContents' in element:
            for toc_element in element.get('tableOfContents').get('content'):
                links.extend(self._find_links_in_element(toc_element))
        
        return links

    def _extract_from_text_run(self, inner_element):
        links = []
        text_run = inner_element.get('textRun')
        if not text_run:
            return links

        # 1. Direct link check
        if text_run.get('textStyle') and text_run.get('textStyle').get('link'):
            url = text_run.get('textStyle').get('link').get('url')
            if url:
                links.append(url)
        
        # 2. Regex search in content (for plain text links)
        text = text_run.get('content', '')
        if text:
            urls = re.findall(r'(https?://[^\s\"\'\>]+)', text)
            links.extend(urls)
            
        return links

    def _extract_youtube_video_id(self, url):
        """Extract video ID from any YouTube URL format.

        Handles:
          - youtube.com/watch?v=VIDEO_ID
          - youtu.be/VIDEO_ID
          - youtube.com/shorts/VIDEO_ID
          - youtube.com/embed/VIDEO_ID
          - youtube.com/v/VIDEO_ID
          - URLs with extra params (?si=, &t=, ?utm_source=, etc.)

        Returns None for non-video URLs (channels, playlists, etc.)
        """
        # Skip non-video URLs early
        non_video_patterns = [
            r'youtube\.com/@',           # Channel handles
            r'youtube\.com/c/',          # Channel old format
            r'youtube\.com/channel/',    # Channel ID format
            r'youtube\.com/user/',       # User pages
            r'youtube\.com/playlist\?',  # Playlists
        ]
        for pattern in non_video_patterns:
            if re.search(pattern, url):
                return None

        # Try each video URL pattern
        video_patterns = [
            r'(?:v=)([0-9A-Za-z_-]{11})',                          # ?v=VIDEO_ID
            r'youtu\.be/([0-9A-Za-z_-]{11})',                      # youtu.be/VIDEO_ID
            r'youtube\.com/shorts/([0-9A-Za-z_-]{11})',             # /shorts/VIDEO_ID
            r'youtube\.com/embed/([0-9A-Za-z_-]{11})',              # /embed/VIDEO_ID
            r'youtube\.com/v/([0-9A-Za-z_-]{11})',                  # /v/VIDEO_ID
        ]
        for pattern in video_patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        return None

    async def scrape_youtube(self, url):
        video_id = self._extract_youtube_video_id(url)

        if not video_id:
            # Determine if it's a known non-video URL type for clearer error message
            if any(x in url for x in ['/@', '/c/', '/channel/', '/user/']):
                return None, "YouTube channel URL (not a video) - skipping"
            if 'playlist' in url:
                return None, "YouTube playlist URL (not a single video) - skipping"
            return None, "Could not extract video ID from YouTube URL"

        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            # Try multiple common patterns for the YouTube API
            transcript_list = None
            if hasattr(YouTubeTranscriptApi, 'get_transcript'):
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
            elif hasattr(YouTubeTranscriptApi, 'list_transcripts'):
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id).find_transcript(['en']).fetch()

            if transcript_list:
                transcript_text = " ".join([item['text'] for item in transcript_list])
                return transcript_text, None
        except Exception as e:
            logging.warning(f"YouTube API failed for {video_id}: {str(e)}. Trying Jina fallback...")

        # JINA FALLBACK for YouTube
        # Normalize to standard watch URL for Jina (works better than short URLs)
        normalized_url = f"https://www.youtube.com/watch?v={video_id}"
        jina_url = f"https://r.jina.ai/{normalized_url}"
        try:
            async with AsyncSession(impersonate="chrome110") as s:
                resp = await s.get(jina_url, timeout=25)
                if resp.status_code == 200 and len(resp.text) > 200:
                    sanitized = self._sanitize_text(resp.text)
                    if not is_block_page(sanitized):
                        return f"[JINA YOUTUBE VERSION] {sanitized}", None
        except Exception as e:
            pass

        # PLAYWRIGHT FALLBACK for YouTube: Scrape the page itself for title, description, comments
        if self.browser_pool:
            try:
                html, pw_error = await scrape_with_playwright(self.browser_pool, normalized_url)
                if html:
                    extracted = self._extract_text_from_html(html)
                    if extracted and not is_block_page(extracted) and len(extracted) > 100:
                        return f"[PLAYWRIGHT YOUTUBE PAGE] {extracted}", None
            except Exception as e:
                logging.debug(f"Playwright YouTube fallback failed: {str(e)}")

        return None, "YouTube transcript unavailable via API, Jina, or Playwright"

    def _clean_url(self, url):
        """Remove UTM and other common tracking parameters that might trigger bot detection."""
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        # List of parameters to remove
        blocked_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid'}
        clean_params = {k: v for k, v in query_params.items() if k.lower() not in blocked_params}
        
        new_query = urlencode(clean_params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _sanitize_text(self, text):
        """Remove control characters and binary junk that might look like invalid characters."""
        if not text:
            return ""
        # Keep printable characters, newlines, and tabs
        # This regex removes most binary/control artifacts
        import string
        printable = set(string.printable + " " + "\n" + "\r" + "\t")
        cleaned = "".join(filter(lambda x: x in printable or ord(x) > 127, text))
        return cleaned.strip()

    async def _extract_pdf_text(self, content_bytes):
        """Extract text from PDF bytes using pypdf."""
        try:
            reader = PdfReader(io.BytesIO(content_bytes))
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text
        except Exception as e:
            return None

    def _extract_text_from_html(self, html):
        """Extract readable text from HTML using trafilatura with BS4 fallback."""
        result = trafilatura.extract(html)
        if result:
            return self._sanitize_text(result)

        soup = BeautifulSoup(html, 'lxml')
        for script in soup(["script", "style"]):
            script.decompose()

        main_content = (soup.find('main') or soup.find('article') or
                       soup.find('div', class_=re.compile(r'content|main|body', re.I)))
        if main_content:
            text = main_content.get_text(separator=' ')
        else:
            text = soup.get_text(separator=' ')

        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = '\n'.join(chunk for chunk in chunks if chunk)

        return self._sanitize_text(text) if text.strip() else None

    def _should_try_playwright(self, last_status_code=None, last_error=None):
        """Determine if a failed curl_cffi attempt warrants a Playwright retry.

        Triggers Playwright for:
          - 401/403: Bot protection / Cloudflare blocking
          - 405: Server rejects curl_cffi's request method
          - 429: Rate limiting (real browser may bypass)
          - 202: Server accepted but didn't return content (JS-rendered SPAs)
          - 500: Server error that may be caused by bot detection
          - Empty extracted text: JS-rendered page that curl_cffi can't render
          - Block/consent page detected: curl_cffi got 200 but content is garbage
        """
        if self.browser_pool is None:
            return False
        if last_status_code in [401, 403, 405, 429, 202, 500]:
            return True
        if last_error and any(phrase in last_error.lower() for phrase in [
            'extracted text was empty',
            'failed after trying',
            'unsupported content-type',
            'block',
            'consent',
            'challenge',
        ]):
            return True
        return False

    async def _get_wayback_url(self, url):
        """Try to find the most recent archived version of a URL on Wayback Machine."""
        api_url = f"https://archive.org/wayback/available?url={url}"
        try:
            async with AsyncSession() as s:
                resp = await s.get(api_url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    closest = data.get("archived_snapshots", {}).get("closest", {})
                    if closest.get("available") and closest.get("url"):
                        return closest["url"]
        except Exception:
            pass
        return None

    async def _try_google_cache(self, url):
        """Try to fetch content from Google's web cache."""
        cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
        try:
            async with AsyncSession(impersonate="chrome120") as s:
                resp = await s.get(cache_url, timeout=15, allow_redirects=True)
                if resp.status_code == 200:
                    extracted = self._extract_text_from_html(resp.text)
                    if extracted and not is_block_page(extracted) and len(extracted) > 200:
                        return extracted
        except Exception as e:
            logging.debug(f"Google Cache failed for {url}: {str(e)}")
        return None

    async def _try_archive_today(self, url):
        """Try to fetch content from archive.today (archive.ph)."""
        archive_api = f"https://archive.ph/newest/{url}"
        try:
            async with AsyncSession(impersonate="chrome120") as s:
                resp = await s.get(archive_api, timeout=15, allow_redirects=True)
                if resp.status_code == 200:
                    extracted = self._extract_text_from_html(resp.text)
                    if extracted and not is_block_page(extracted) and len(extracted) > 200:
                        return extracted
        except Exception as e:
            logging.debug(f"archive.today failed for {url}: {str(e)}")
        return None

    async def _try_sciencedirect_abstract(self, url):
        """Try to extract ScienceDirect paper metadata via APIs when Cloudflare blocks direct access."""
        # Extract PII from ScienceDirect URL
        pii_match = re.search(r'/pii/([A-Z0-9]+)', url, re.IGNORECASE)
        if not pii_match:
            return None
        pii = pii_match.group(1)

        # Try Semantic Scholar API (free, no auth needed)
        try:
            sem_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:10.1016/{pii[:4]}.{pii[4:]}?fields=title,abstract,authors,year,citationCount"
            async with AsyncSession() as s:
                resp = await s.get(sem_url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    parts = []
                    if data.get('title'):
                        parts.append(f"Title: {data['title']}")
                    if data.get('authors'):
                        authors = ', '.join(a.get('name', '') for a in data['authors'][:10])
                        parts.append(f"Authors: {authors}")
                    if data.get('year'):
                        parts.append(f"Year: {data['year']}")
                    if data.get('abstract'):
                        parts.append(f"Abstract: {data['abstract']}")
                    if data.get('citationCount'):
                        parts.append(f"Citations: {data['citationCount']}")
                    if parts:
                        return '\n'.join(parts)
        except Exception as e:
            logging.debug(f"Semantic Scholar failed for {pii}: {str(e)}")

        # Try CrossRef API as backup
        try:
            cr_url = f"https://api.crossref.org/works?query.bibliographic={pii}&rows=1"
            async with AsyncSession() as s:
                resp = await s.get(cr_url, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get('message', {}).get('items', [])
                    if items:
                        item = items[0]
                        parts = []
                        if item.get('title'):
                            parts.append(f"Title: {item['title'][0]}")
                        if item.get('author'):
                            authors = ', '.join(f"{a.get('given', '')} {a.get('family', '')}" for a in item['author'][:10])
                            parts.append(f"Authors: {authors}")
                        if item.get('abstract'):
                            parts.append(f"Abstract: {item['abstract']}")
                        if parts:
                            return '\n'.join(parts)
        except Exception as e:
            logging.debug(f"CrossRef failed for {pii}: {str(e)}")

        return None

    async def scrape_general(self, url):
        clean_url = self._clean_url(url)

        # Multiple impersonation targets to try
        # NOTE: firefox107 removed — no longer supported by curl_cffi
        fingerprints = ["chrome110", "safari15_5", "chrome120"]

        # Track last failure reason for Playwright fallback decision
        last_status_code = None
        last_error = None

        for fp in fingerprints:
            try:
                # Add a small random jitter to avoid rapid-fire detection
                await asyncio.sleep(random.uniform(0.5, 1.5))

                headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.google.com/",
                    "DNT": "1",
                    "Upgrade-Insecure-Requests": "1",
                    # Modern Client Hints to look more "human"
                    "sec-ch-ua": '"Not A;Brand";v="99", "Chromium";v="110", "Google Chrome";v="110"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-User": "?1"
                }

                async with AsyncSession(impersonate=fp) as s:
                    response = await s.get(clean_url, timeout=25, allow_redirects=True, headers=headers)

                    if response.status_code == 200:
                        content_type = response.headers.get("Content-Type", "").lower()

                        # Handle PDFs
                        if "application/pdf" in content_type or clean_url.endswith(".pdf"):
                            logging.info(f"Detected PDF content at {clean_url}")
                            text = await self._extract_pdf_text(response.content)
                            if text:
                                return self._sanitize_text(text), None
                            return None, "Failed to extract text from PDF"

                        # Handle HTML/Text
                        if "text/html" in content_type or "text/plain" in content_type:
                            html = response.text
                            extracted = self._extract_text_from_html(html)
                            if extracted and not is_block_page(extracted):
                                return extracted, None
                            elif extracted:
                                last_error = "Extracted text was a block/consent page"
                                logging.warning(f"curl_cffi got block page from {clean_url} with {fp}, trying next...")
                                continue
                            else:
                                last_error = "Extracted text was empty"
                                continue  # Try next fingerprint before giving up

                        return None, f"Unsupported Content-Type: {content_type}"

                    elif response.status_code == 429:
                        # Rate limited — wait and retry with next fingerprint
                        last_status_code = response.status_code
                        logging.warning(f"Got 429 (rate limited) for {clean_url} with {fp}, waiting 5s before next fingerprint...")
                        await asyncio.sleep(5)
                        continue

                    elif response.status_code in [401, 403, 405, 202, 500]:
                        # These status codes may be fixable by Playwright or next fingerprint
                        last_status_code = response.status_code
                        logging.warning(f"Got {response.status_code} for {clean_url} with {fp}, trying next fingerprint...")
                        continue # Try next fingerprint, then Playwright
                    else:
                        # Genuine errors (404, etc.) - no point retrying
                        last_status_code = response.status_code
                        return None, f"HTTP Error {response.status_code}"
            except Exception as e:
                last_error = str(e)
                logging.error(f"Error scraping {clean_url} with {fp}: {str(e)}")
                continue

        # PLAYWRIGHT FALLBACK: For sites with JS challenges (Cloudflare, etc.)
        if self._should_try_playwright(last_status_code, last_error):
            logging.info(f"Trying Playwright (headless browser) fallback for {clean_url}...")
            try:
                html, pw_error = await scrape_with_playwright(self.browser_pool, clean_url)
                if html:
                    extracted = self._extract_text_from_html(html)
                    if extracted and not is_block_page(extracted):
                        return f"[PLAYWRIGHT] {extracted}", None
                    elif extracted:
                        logging.warning(f"Playwright returned a block/error page for {clean_url}, falling through...")
                    else:
                        logging.warning(f"Playwright got HTML but extraction yielded no text for {clean_url}")
                else:
                    logging.warning(f"Playwright fallback failed for {clean_url}: {pw_error}")
            except Exception as e:
                logging.error(f"Playwright error for {clean_url}: {str(e)}")

        # SECONDARY FALLBACK: Jina Reader (Very effective for G2/TrustRadius)
        logging.info(f"Trying Jina Reader fallback for {clean_url}...")
        jina_url = f"https://r.jina.ai/{clean_url}"
        try:
            async with AsyncSession(impersonate="chrome110") as s:
                resp = await s.get(jina_url, timeout=25)
                if resp.status_code == 200 and len(resp.text) > 200:
                    sanitized = self._sanitize_text(resp.text)
                    if not is_block_page(sanitized):
                        return f"[JINA READER VERSION] {sanitized}", None
                    else:
                        logging.warning(f"Jina Reader returned a block/error page for {clean_url}, falling through...")
        except Exception as e:
            logging.error(f"Jina Reader failed for {clean_url}: {str(e)}")

        # GOOGLE CACHE FALLBACK: Often has recent copies of pages
        logging.info(f"Trying Google Cache fallback for {clean_url}...")
        cache_result = await self._try_google_cache(clean_url)
        if cache_result:
            return f"[GOOGLE CACHE] {cache_result}", None

        # ARCHIVE.TODAY FALLBACK: Community-maintained archive, good for paywalled content
        logging.info(f"Trying archive.today fallback for {clean_url}...")
        archive_result = await self._try_archive_today(clean_url)
        if archive_result:
            return f"[ARCHIVE.TODAY] {archive_result}", None

        # ULTIMATE FALLBACK: Wayback Machine
        logging.info(f"All other methods failed for {clean_url}. Trying Wayback Machine fallback...")
        wayback_url = await self._get_wayback_url(clean_url)
        if wayback_url:
            try:
                async with AsyncSession(impersonate="chrome110") as s:
                    resp = await s.get(wayback_url, timeout=20)
                    if resp.status_code == 200:
                        result = trafilatura.extract(resp.text)
                        if result:
                            return f"[ARCHIVED VERSION] {self._sanitize_text(result)}", None
            except Exception as e:
                logging.error(f"Wayback fallback failed for {clean_url}: {str(e)}")

        return None, "Failed after trying all methods (curl_cffi, Playwright, Jina, Google Cache, archive.today, Wayback)"

    def _convert_reddit_url(self, url):
        """Convert any Reddit URL to old.reddit.com for reliable scraping.

        old.reddit.com serves plain HTML without heavy JS/React, making it
        scrapable with curl_cffi without needing Playwright.
        """
        parsed = urlparse(url)
        if parsed.hostname in ['www.reddit.com', 'reddit.com', 'new.reddit.com']:
            return urlunparse(parsed._replace(netloc='old.reddit.com'))
        return url

    async def scrape_reddit(self, url):
        """Scrape Reddit via old.reddit.com, JSON API, Jina, .compact, and Playwright fallbacks."""
        old_url = self._convert_reddit_url(url)
        logging.info(f"Reddit detected. Using old.reddit.com: {old_url}")

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        }

        # Method 1: old.reddit.com HTML
        try:
            async with AsyncSession(impersonate="chrome110") as s:
                response = await s.get(old_url, timeout=25, allow_redirects=True, headers=headers)
                if response.status_code == 200:
                    extracted = self._extract_text_from_html(response.text)
                    if extracted and not is_block_page(extracted):
                        return f"[REDDIT] {extracted}", None
        except Exception as e:
            logging.warning(f"Reddit old.reddit.com failed: {str(e)}")

        # Method 2: Reddit JSON API (append .json to the URL)
        # Works for most post URLs and bypasses HTML rendering entirely
        try:
            json_url = old_url.rstrip('/') + '.json'
            json_headers = {
                "User-Agent": "Mozilla/5.0 (research-link-scraper; academic)",
                "Accept": "application/json",
            }
            async with AsyncSession() as s:
                resp = await s.get(json_url, timeout=20, headers=json_headers)
                if resp.status_code == 200:
                    import json
                    data = resp.json()
                    parts = []
                    # Extract post title and selftext
                    if isinstance(data, list) and len(data) > 0:
                        post_data = data[0].get('data', {}).get('children', [{}])[0].get('data', {})
                        title = post_data.get('title', '')
                        selftext = post_data.get('selftext', '')
                        if title:
                            parts.append(f"Title: {title}")
                        if selftext:
                            parts.append(f"Post: {selftext}")
                        # Extract top comments
                        if len(data) > 1:
                            comments = data[1].get('data', {}).get('children', [])
                            for c in comments[:10]:  # Top 10 comments
                                cdata = c.get('data', {})
                                body = cdata.get('body', '')
                                if body:
                                    parts.append(f"Comment: {body}")
                    if parts:
                        text = '\n\n'.join(parts)
                        return f"[REDDIT JSON] {self._sanitize_text(text)}", None
        except Exception as e:
            logging.warning(f"Reddit JSON API failed: {str(e)}")

        # Method 3: Jina Reader
        jina_url = f"https://r.jina.ai/{url}"
        try:
            async with AsyncSession(impersonate="chrome110") as s:
                resp = await s.get(jina_url, timeout=25)
                if resp.status_code == 200 and len(resp.text) > 200:
                    sanitized = self._sanitize_text(resp.text)
                    if not is_block_page(sanitized):
                        return f"[JINA REDDIT VERSION] {sanitized}", None
                    else:
                        logging.warning(f"Jina Reddit returned a block/error page, falling through...")
        except Exception as e:
            logging.warning(f"Jina Reddit fallback failed: {str(e)}")

        # Method 4: .compact mobile view (lightweight, often less blocked)
        try:
            compact_url = old_url.rstrip('/') + '/.compact'
            async with AsyncSession(impersonate="chrome120") as s:
                resp = await s.get(compact_url, timeout=20, allow_redirects=True, headers=headers)
                if resp.status_code == 200:
                    extracted = self._extract_text_from_html(resp.text)
                    if extracted and not is_block_page(extracted):
                        return f"[REDDIT COMPACT] {extracted}", None
        except Exception as e:
            logging.warning(f"Reddit .compact fallback failed: {str(e)}")

        # Method 5: Playwright for Reddit (use www.reddit.com, NOT old.reddit.com)
        # Playwright can handle JS-rendered Reddit; old.reddit.com blocks headless browsers
        if self.browser_pool:
            try:
                # Use the original www.reddit.com URL for Playwright
                parsed = urlparse(url)
                www_url = urlunparse(parsed._replace(netloc='www.reddit.com'))
                await asyncio.sleep(2)  # Extra delay to avoid rate detection
                html, pw_error = await scrape_with_playwright(self.browser_pool, www_url)
                if html:
                    extracted = self._extract_text_from_html(html)
                    if extracted and not is_block_page(extracted):
                        return f"[PLAYWRIGHT REDDIT] {extracted}", None
            except Exception as e:
                logging.error(f"Playwright Reddit fallback failed: {str(e)}")

        # Method 6: Google Cache as last resort for Reddit
        cache_result = await self._try_google_cache(url)
        if cache_result:
            return f"[GOOGLE CACHE REDDIT] {cache_result}", None

        return None, "Reddit scraping failed via old.reddit.com, JSON API, Jina, compact, Playwright, and Google Cache"

    async def process_link(self, url):
        logging.info(f"Processing: {url}")
        content = None
        error = None

        # Route to specialized scrapers based on domain
        if "youtube.com" in url or "youtu.be" in url:
            content, error = await self.scrape_youtube(url)
        elif "reddit.com" in url:
            content, error = await self.scrape_reddit(url)
        elif "linkedin.com" in url:
            # LinkedIn blocks all scraping. Try Jina first (best chance), then general.
            jina_url = f"https://r.jina.ai/{url}"
            try:
                async with AsyncSession(impersonate="chrome110") as s:
                    resp = await s.get(jina_url, timeout=25)
                    if resp.status_code == 200 and len(resp.text) > 200:
                        sanitized = self._sanitize_text(resp.text)
                        if not is_block_page(sanitized):
                            content = f"[JINA LINKEDIN] {sanitized}"
                        else:
                            logging.warning(f"Jina LinkedIn returned a block/error page for {url}")
            except Exception:
                pass
            if not content:
                content, error = await self.scrape_general(url)
        elif "reuters.com" in url:
            # Reuters has aggressive paywall. Try archive.today first, then general.
            logging.info(f"Reuters detected. Trying archive.today first for {url}...")
            archive_result = await self._try_archive_today(url)
            if archive_result:
                content = f"[ARCHIVE.TODAY REUTERS] {archive_result}"
            else:
                # Try Google Cache
                cache_result = await self._try_google_cache(url)
                if cache_result:
                    content = f"[GOOGLE CACHE REUTERS] {cache_result}"
                else:
                    content, error = await self.scrape_general(url)
        elif "sciencedirect.com" in url:
            # ScienceDirect has unbeatable Cloudflare. Try abstract APIs first.
            logging.info(f"ScienceDirect detected. Trying academic APIs for metadata...")
            api_result = await self._try_sciencedirect_abstract(url)
            if api_result:
                content = f"[ACADEMIC API] {api_result}"
            else:
                content, error = await self.scrape_general(url)
        else:
            content, error = await self.scrape_general(url)

        # FINAL GATE: Reject block pages, cookie consent, login walls, etc.
        # This catches garbage content regardless of which method produced it.
        if content and is_block_page(content):
            logging.warning(f"FINAL GATE REJECTED content for {url} (block/consent/login page detected)")
            content = None
            error = "Content was a block page, cookie consent, or login wall"

        if content:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(f"\n\n--- CONTENT FROM: {url} ---\n\n")
                f.write(content)
            logging.info(f"Successfully scraped: {url}")
        else:
            with open(self.failed_log, "a", encoding="utf-8") as f:
                f.write(f"{url} - Error: {error}\n")
            logging.warning(f"Failed to scrape: {url} - {error}")

    async def run(self, doc_url, target_tab_id=None):
        # Extract ID from URL
        match = re.search(r'/d/([^/]+)', doc_url)
        if not match:
            logging.error("Invalid Google Doc URL")
            return
        
        doc_id = match.group(1)
        
        # If target_tab_id不是直接传进来的，从URL里提取
        if not target_tab_id:
            tab_match = re.search(r'[#?&]tab=([^&?#]+)', doc_url)
            target_tab_id = tab_match.group(1) if tab_match else None
        
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        
        # Set unique filenames for this run
        self.output_file = f"scraped_content_{timestamp}.md"
        self.failed_log = f"failed_links_{timestamp}.log"

        logging.info(f"Unique output file for this session: {self.output_file}")

        # Initialize files
        with open(self.output_file, "w", encoding="utf-8") as f:
            f.write(f"# SCRAPE SESSION FOR DOC {doc_id} (Tab: {target_tab_id or 'Auto/Interactive'})\n**START:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        with open(self.failed_log, "w", encoding="utf-8") as f:
            f.write(f"--- FAILED LINKS SESSION FOR DOC {doc_id}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

        logging.info(f"Fetching document: {doc_id}")
        
        doc_data = self.get_doc_content(doc_id)
        if not doc_data:
            return
        # Handle Tabs vs Body
        content_elements = []
        if 'tabs' in doc_data:
            # Flatten all tabs for easy selection
            all_tabs = []
            def collect_tabs(tabs_list):
                for t in tabs_list:
                    all_tabs.append(t)
                    if 'childTabs' in t:
                        collect_tabs(t['childTabs'])
            collect_tabs(doc_data['tabs'])

            found_tab = None

            if target_tab_id:
                # User provided a tab ID (via URL or separate arg)
                logging.info(f"Targeting specific Tab ID: {target_tab_id}")
                for t in all_tabs:
                    if t.get('tabProperties', {}).get('tabId') == target_tab_id:
                        found_tab = t
                        break
                if not found_tab:
                    logging.warning(f"Tab ID {target_tab_id} not found in document.")
            
            # If no tab found yet, try interactive or default
            if not found_tab:
                import sys
                if sys.stdin.isatty():
                    # Interactive Mode
                    print("\n--- MULTIPLE TABS DETECTED ---", flush=True)
                    print("The scraping URL didn't specify a tab (or quotes were missing in the command).")
                    print("Please select which tab you want to scrape:\n", flush=True)
                    
                    for i, tab in enumerate(all_tabs):
                        title = tab.get('tabProperties', {}).get('title', 'Untitled')
                        tid = tab.get('tabProperties', {}).get('tabId')
                        print(f"[{i + 1}] {title} (ID: {tid})", flush=True)
                    
                    print(f"[{len(all_tabs) + 1}] SCRAPE ALL TABS", flush=True)

                    try:
                        choice = input(f"\nEnter choice (1-{len(all_tabs) + 1}): ").strip()
                    except EOFError:
                        choice = ""
                    
                    if choice == str(len(all_tabs) + 1):
                        # Scrape ALL tabs
                        logging.info("User selected to scrape ALL tabs.")
                        content_elements = [] 
                        for t in all_tabs:
                            if 'documentTab' in t:
                                tab_content = t['documentTab'].get('body', {}).get('content', [])
                                content_elements.extend(tab_content)
                        found_tab = "ALL_TABS" 
                    elif choice.isdigit() and 1 <= int(choice) <= len(all_tabs):
                        found_tab = all_tabs[int(choice) - 1]
                    else:
                        logging.warning("No valid choice made. Defaulting to first tab.")
                        found_tab = all_tabs[0]
                else:
                    # Non-interactive Mode (default for background/scripts)
                    logging.info("Non-interactive session: Multiple tabs found but no Tab ID effectively passed.")
                    logging.info("Defaulting to the FIRST tab only. To target others, use Option 2 or 3 below.")
                    found_tab = all_tabs[0]

            if found_tab == "ALL_TABS":
                 pass # content_elements already populated
            elif found_tab and 'documentTab' in found_tab:
                content_elements = found_tab['documentTab'].get('body', {}).get('content', [])
                logging.info(f"Using content from tab: {found_tab.get('tabProperties', {}).get('title')}")
            else:
                logging.error("Could not find document content in selected tab.")
                return
        else:
            # Traditional single-tab document
            content_elements = doc_data.get('body', {}).get('content', [])

        if not content_elements:
            logging.error("No content found in the document body or tab.")
            return

        links = self.extract_links_from_content(content_elements)
        logging.info(f"Found {len(links)} links to process.")

        tasks = [self.process_link(link) for link in links]
        await asyncio.gather(*tasks)
        logging.info("Scraping task completed.")

    def extract_links_from_content(self, content):
        links = []
        logging.info(f"Processing {len(content)} top-level elements.")
        
        for element in content:
            links.extend(self._find_links_in_element(element))
            
        return list(set(links)) # Unique links

    def get_all_links_from_doc(self, doc_id):
        """
        Scans the entire Google Doc and extracts all links found in the content.
        """
        doc = self.get_doc_content(doc_id)
        if not doc:
            return []

        content = []
        
        # 1. Add Main Body
        if 'body' in doc and 'content' in doc['body']:
            content.extend(doc['body']['content'])
            
        # 2. Add All Tabs (recursively)
        if 'tabs' in doc:
            def collect_tabs(tabs_list):
                for t in tabs_list:
                    if 'documentTab' in t:
                        content.extend(t['documentTab'].get('body', {}).get('content', []))
                    if 'childTabs' in t:
                        collect_tabs(t['childTabs'])
            collect_tabs(doc['tabs'])

        logging.info(f"Scanning {len(content)} elements for ALL links...")

        links = []
        for element in content:
            links.extend(self._find_links_in_element(element))

        return list(set(links))

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <google_doc_url> [tab_id]")
    else:
        url = sys.argv[1]
        tab = sys.argv[2] if len(sys.argv) > 2 else None
        scraper = DocScraper()
        asyncio.run(scraper.run(url, tab))
