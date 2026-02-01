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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [SERVICE] - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.SERVICE_LOG_FILE),
        logging.StreamHandler()
    ]
)

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
        try:
            worksheet = self.get_worksheet()
            
            # Get all values
            records = worksheet.get_all_records()
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
                    # Mark as processing immediately to avoid double-process
                    try:
                        worksheet.update_cell(i, result_col_idx + 1, "Processing...")
                    except Exception as e:
                        logging.error(f"Failed to mark Row {i} as Processing: {e}")
                        continue
                        
                    self.process_task(worksheet, i, draft_link, result_col_idx + 1) # +1 for 1-based index
                    
        except Exception as e:
            logging.error(f"Error checking sheet: {e}")

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

        try:
            asyncio.run(run_scrape())
        except Exception as e:
             logging.error(f"Scraping execution error: {e}")
             worksheet.update_cell(row_num, result_col_index, "Error: Scraping execution failed")
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
                except Exception as e:
                    logging.error(f"Failed to update sheet for Row {row_num}: {e}")

                # 5. Cleanup Local File
                try:
                    os.remove(filepath)
                    logging.info(f"Deleted local file: {filename}")
                except Exception as e:
                    logging.warning(f"Failed to delete local file: {e}")
            else:
                 worksheet.update_cell(row_num, result_col_index, "Error: Drive Upload Failed")
        else:
             worksheet.update_cell(row_num, result_col_index, "Error: No content scraped")
             
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

def main():
    print("--- Starting Research Link Scraper Service (Production) ---")
    print(f"Monitoring Sheet ID: {config.SPREADSHEET_ID}")
    print("Press Ctrl+C to stop.")
    
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
            wait_time = min(config.POLL_INTERVAL * (2 ** (error_count - 1)), 300) # Max 5 min wait
            logging.error(f"Critical Service Error: {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)
            continue
            
        time.sleep(config.POLL_INTERVAL)

if __name__ == "__main__":
    main()
