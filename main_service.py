import asyncio
import os
import sys
import logging
import time
import gspread
import re
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from datetime import datetime
import urllib.request
import threading

# Import existing scraper class
# Ensure dependencies are installed: gspread
try:
    from scraper import DocScraper
except ImportError:
    print("Error: scraper.py not found or dependencies missing.")
    sys.exit(1)

# Import configuration
import config

# Configuration accessed via config.py
# SPREADSHEET_ID and POLL_INTERVAL are now in config.py

# Dashboard State
class ServiceState:
    def __init__(self):
        self.start_time = time.time()
        self.processed_count = 0
        self.error_count = 0
        self.last_activity = "Initializing..."
        self.recent_logs = []
        
        # Advanced Metrics
        self.total_duration_seconds = 0.0
        self.last_success_time = None
        self.next_poll_time = None

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"{timestamp} - {message}"
        self.recent_logs.append(entry)
        if len(self.recent_logs) > 50:
            self.recent_logs.pop(0)

dashboard_state = ServiceState()

class InMemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            log_entry = self.format(record)
            dashboard_state.recent_logs.append(log_entry)
            if len(dashboard_state.recent_logs) > 50:
                dashboard_state.recent_logs.pop(0)
            
            # Sync Error Count with actual Error Logs
            if record.levelno >= logging.ERROR:
                dashboard_state.error_count += 1
        except Exception:
            self.handleError(record)

# Setup logging - Force handlers to ensure valid capture
# (basicConfig can be ignored if a library has already set up logging)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Clear existing handlers to avoid duplicates during reloads (or if libs added default ones)
if root_logger.handlers:
    root_logger.handlers = []

formatter = logging.Formatter('%(asctime)s - [SERVICE] - %(levelname)s - %(message)s')

# 1. File Handler
file_handler = logging.FileHandler(config.SERVICE_LOG_FILE)
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

# 2. Console Handler
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
root_logger.addHandler(stream_handler)

# 3. Dashboard Memory Handler
memory_handler = InMemoryLogHandler()
memory_handler.setFormatter(formatter)
root_logger.addHandler(memory_handler)

logging.info("Logging system initialized. Dashboard handler active.")

class ResearchService:
    def __init__(self):
        self.scraper = DocScraper()
        self.creds = self.scraper.creds
        self.gc = gspread.authorize(self.creds)
        self.drive_service = build('drive', 'v3', credentials=self.creds)
        
    def get_worksheet(self):
        """Helper to get fresh worksheet object with error handling"""
        try:
            sh = self.gc.open_by_key(config.SPREADSHEET_ID)
            return sh.get_worksheet(0)
        except gspread.exceptions.APIError as e:
            if e.response.status_code in [401, 403]:
                logging.warning("Auth expired or failed. Refreshing internal client...")
                # Re-authorize
                self.scraper = DocScraper() # detailed re-auth inside
                self.creds = self.scraper.creds
                self.gc = gspread.authorize(self.creds)
                self.drive_service = build('drive', 'v3', credentials=self.creds)
                # Retry once
                sh = self.gc.open_by_key(config.SPREADSHEET_ID)
                return sh.get_worksheet(0)
            raise e

    def check_and_process(self):
        logging.info("Checking Google Sheet for new tasks...")
        dashboard_state.last_activity = "Checking for new tasks..."
        try:
            worksheet = self.get_worksheet()
            
            # Get all values
            all_values = worksheet.get_all_values()
            if not all_values:
                logging.warning("Sheet appears empty.")
                return

            header = all_values[0]
            try:
                link_col_idx = header.index(config.INPUT_COLUMN_NAME)
                result_col_idx = header.index(config.OUTPUT_COLUMN_NAME)
            except ValueError as e:
                logging.error(f"Missing required columns in Sheet: {e}")
                dashboard_state.error_count += 1
                return

            # Iterate rows (skip header)
            for i, row in enumerate(all_values[1:], start=2):
                # Check bounds
                if len(row) <= link_col_idx: continue
                
                draft_link = row[link_col_idx].strip()
                # Check status (Scraped Data column)
                current_status = row[result_col_idx].strip() if len(row) > result_col_idx else ""
                
                if draft_link and not current_status:
                    logging.info(f"Found new task at Row {i}: {draft_link}")
                    dashboard_state.last_activity = f"Processing Row {i}..."
                    # Mark as processing immediately to avoid double-process
                    try:
                        worksheet.update_cell(i, result_col_idx + 1, "Processing...")
                    except Exception as e:
                        logging.error(f"Failed to mark Row {i} as Processing: {e}")
                        continue
                        
                    self.process_task(worksheet, i, draft_link, result_col_idx + 1) # +1 for 1-based index
                    
        except Exception as e:
            logging.error(f"Error checking sheet: {e}")
            dashboard_state.error_count += 1

    def process_task(self, worksheet, row_num, draft_link, result_col_index):
        # 0. Check if it's a Spreadsheet (common mistake)
        if "/spreadsheets/" in draft_link:
             msg = "Error: Input is a Google Sheet, but this tool scrapes Google Docs."
             logging.error(f"Row {row_num}: {msg}")
             try:
                 worksheet.update_cell(row_num, result_col_index, msg)
             except Exception as e:
                 logging.error(f"Failed to update sheet: {e}")
             return

        # 1. Extract Doc ID
        match = re.search(r'/d/([a-zA-Z0-9-_]+)', draft_link)
        if not match:
            # Report invalid URL instead of silent skip
            msg = "Error: Invalid Doc URL format"
            logging.error(f"Row {row_num}: {msg} - Link: {draft_link}")
            try:
                worksheet.update_cell(row_num, result_col_index, msg)
            except Exception as e:
                logging.error(f"Failed to update sheet for invalid URL at Row {row_num}: {e}")
            return

        doc_id = match.group(1)
        
        # 2. Scrape Items
        logging.info(f"Scraping Doc ID: {doc_id} for ALL links...")
        try:
            links = self.scraper.get_all_links_from_doc(doc_id)
        except Exception as e:
            logging.error(f"Scraper Error for {doc_id}: {e}")
            worksheet.update_cell(row_num, result_col_index, f"Error: Scraper Failed - {str(e)[:50]}")
            return
        
        if not links:
            logging.warning(f"No links found in doc {doc_id}")
            worksheet.update_cell(row_num, result_col_index, "No links found or empty")
            return

        logging.info(f"Found {len(links)} links. Scraping content...")
        
        # Prepare output file
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f"scraped_data_{doc_id}_{timestamp}.md"
        filepath = os.path.abspath(filename)
        
        # Redirect scraper output to this file
        self.scraper.output_file = filepath
        # Clear/Create file
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# Source Doc: {draft_link}\n")
            f.write(f"**Scraped Date:** {datetime.now()}\n\n")

        # Run async scraping
        async def run_scrape():
            tasks = [self.scraper.process_link(link) for link in links]
            await asyncio.gather(*tasks)

        # Track duration
        start_time = time.time()

        try:
            asyncio.run(run_scrape())
        except Exception as e:
             logging.error(f"Scraping execution error: {e}")
             worksheet.update_cell(row_num, result_col_index, "Error: Scraping execution failed")
             dashboard_state.error_count += 1
             return
        
        # 3. Upload to Drive
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            logging.info(f"Uploading {filename} to Drive...")
            drive_link = self.upload_to_drive(filepath, filename)
            
            if drive_link:
                # 4. Update Sheet
                try:
                    worksheet.update_cell(row_num, result_col_index, drive_link)
                    logging.info(f"Task Complete. Updated Sheet Row {row_num}.")
                    
                    # Update Metrics
                    dashboard_state.processed_count += 1
                    dashboard_state.last_activity = "Task Complete"
                    dashboard_state.last_success_time = time.time()
                    duration = time.time() - start_time
                    dashboard_state.total_duration_seconds += duration
                    
                except Exception as e:
                    logging.error(f"Failed to update sheet for Row {row_num}: {e}")
                    dashboard_state.error_count += 1

                # 5. Cleanup Local File
                try:
                    os.remove(filepath)
                    logging.info(f"Deleted local file: {filename}")
                except Exception as e:
                    logging.warning(f"Failed to delete local file: {e}")
            else:
                 worksheet.update_cell(row_num, result_col_index, "Error: Drive Upload Failed")
                 dashboard_state.error_count += 1
        else:
             worksheet.update_cell(row_num, result_col_index, "Error: No content scraped")
             dashboard_state.error_count += 1
             
    def upload_to_drive(self, filepath, filename):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                file_metadata = {
                    'name': filename,
                    'parents': [config.SCOPED_DATA_FOLDER_ID]
                }
                media = MediaFileUpload(filepath, mimetype='text/markdown')
                
                file = self.drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields='id, webViewLink, webContentLink'
                ).execute()
                
                # Make it shareable (anyone with link) - Optional but usually needed for easy access
                # or just rely on the user owning the file. 
                # Let's add permission to be safe if the user wants to share it.
                # self.drive_service.permissions().create(
                #     fileId=file.get('id'),
                #     body={'type': 'anyone', 'role': 'reader'}
                # ).execute()

                return file.get('webViewLink')
            except Exception as e:
                logging.warning(f"Drive Upload Error (Attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1)) # Backoff: 2s, 4s
                else:
                    logging.error(f"Drive Upload Failed after {max_retries} attempts.")
                    return None


def start_keep_alive_server():
    """Starts a web server to serve the Dashboard and satisfy Render's port binding."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading
    import json

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                # Serve the embedded dashboard or read from file
                try:
                    with open('dashboard_template.html', 'r', encoding='utf-8') as f:
                        self.wfile.write(f.read().encode('utf-8'))
                except FileNotFoundError:
                    self.wfile.write(b"<h1>Dashboard Error</h1><p>Template not found.</p>")
            
            elif self.path == '/api/status':
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                
                uptime = time.time() - dashboard_state.start_time
                stats = {
                    'uptime_seconds': uptime,
                    'processed_count': dashboard_state.processed_count,
                    'error_count': dashboard_state.error_count,
                    'last_activity': dashboard_state.last_activity,
                    'total_duration_seconds': dashboard_state.total_duration_seconds,
                    'last_success_time': dashboard_state.last_success_time,
                    'next_poll_time': dashboard_state.next_poll_time,
                    'recent_logs': list(dashboard_state.recent_logs) # Copy list
                }
                self.wfile.write(json.dumps(stats).encode('utf-8'))
            
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
        
        # Suppress log messages to keep console clean
        def log_message(self, format, *args):
            pass

    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), DashboardHandler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        print(f"--- Dashboard & Keep-alive server started on port {port} ---")
    except Exception as e:
        print(f"Warning: Failed to start web server: {e}")

def start_self_ping():
    """
    Background thread to ping the application's own external URL to prevent Render spin-down.
    Render free instances spin down after 15 minutes of inactivity.
    Pinging every 14 minutes (840 seconds) keeps it active.
    """
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        print("Self-ping: RENDER_EXTERNAL_URL not set. Skipping keep-alive ping (Local Mode).")
        return

    # Ensure URL starts with http
    if not url.startswith("http"):
        url = f"https://{url}"

    print(f"Self-ping: Active for {url} every 14 minutes.")

    def pinger():
        while True:
            # Wait 14 minutes (840 seconds)
            time.sleep(840)
            try:
                # Add a timestamp to avoid caching (optional but good practice)
                ping_url = f"{url}/api/status?ping={int(time.time())}"
                with urllib.request.urlopen(ping_url, timeout=10) as response:
                     status = response.getcode()
                     print(f"[{datetime.now().strftime('%H:%M:%S')}] Keep-alive ping sent to {url}. Status: {status}")
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Keep-alive ping failed: {e}")

    thread = threading.Thread(target=pinger, daemon=True)
    thread.start()

def main():
    print("--- Starting Research Link Scraper Service (Production) ---")
    print(f"Monitoring Sheet ID: {config.SPREADSHEET_ID}")
    print("Press Ctrl+C to stop.")
    
    # Start the keep-alive server
    start_keep_alive_server()

    # Start the self-ping mechanism
    start_self_ping()
    
    
    service = ResearchService()
    
    # Simple exponential backoff for main loop
    error_count = 0
    
    while True:
        try:
            service.check_and_process()
            error_count = 0 # reset on success
        except KeyboardInterrupt:
            print("Stopping...")
            sys.exit(0)
        except Exception as e:
            error_count += 1
            dashboard_state.error_count += 1
            dashboard_state.last_activity = "Recovering from error..."
            wait_time = min(config.POLL_INTERVAL * (2 ** (error_count - 1)), 300) # Max 5 min wait
            logging.error(f"Critical Service Error: {e}. Retrying in {wait_time}s...")
            dashboard_state.next_poll_time = time.time() + wait_time
            time.sleep(wait_time)
            continue
            
        dashboard_state.next_poll_time = time.time() + config.POLL_INTERVAL
        time.sleep(config.POLL_INTERVAL)

if __name__ == "__main__":
    main()
