
# ==========================================
# RESEARCH LINK SCRAPER CONFIGURATION
# ==========================================

# 1. GOOGLE SHEET SETTINGS
# The ID is the long string in your Google Sheet URL: .../d/THIS_IS_THE_ID/edit...
# The ID is the long string in your Google Sheet URL: .../d/THIS_IS_THE_ID/edit...
SPREADSHEET_ID = "1n7Vx1BOeUHqh_UTAghBef5IxGMdMeKXyAxUh8ZS1MmI"

# The name of the tab in the spreadsheet to monitor (usually "Sheet1")
SHEET_NAME = "Sheet1"

# Column Headers (Configurable)
INPUT_COLUMN_NAME = "Post Topic Research Draft"
OUTPUT_COLUMN_NAME = "Scraped Data"

# 2. FILE PATHS
# Path to your Google Cloud credentials JSON file (downloaded from Cloud Console)
CREDENTIALS_FILE = "credentials.json"

# Path where the authentication token will be saved (generated automatically after first login)
TOKEN_FILE = "token.json"

# Log file for the background service
SERVICE_LOG_FILE = "service.log"

# Log file for the scraping process
SCRAPER_LOG_FILE = "scraping_log.log"

# 3. SETTINGS
# How often to check the sheet for new links (in seconds)
POLL_INTERVAL = 1800

# 4. OUTPUT SETTINGS
# Google Drive Folder ID to upload scraped files to
SCOPED_DATA_FOLDER_ID = "1vGkpGQakXrhNfk_YJn2SZUMdcZQTLQXk"
