# Research Link Scraper

**Production-ready automated research assistant** that monitors a Google Sheet for scraping tasks, extracts content from links (including Google Docs, YouTube, PDFs, and general web pages), and saves the results as clean Markdown files to your Google Drive.

## Features

- **Automated Monitoring**: Polling a Google Sheet for new links every 30 minutes.
- **Universal Scraping**:
  - **Google Docs**: Deep extraction including nested tables and tabs.
  - **YouTube**: Transcripts via API or Jina AI fallback.
  - **Web**: Advanced stealth scraping with `curl_cffi` (impersonating Chrome/Safari) and `trafilatura`.
  - **PDFs**: Text extraction from PDF files.
- **Resilient**: Multiple fallback strategies (Impersonation -> Jina AI -> Wayback Machine).
- **Clean Output**: Saves all data as formatted Markdown files in a specific Google Drive folder.
- **Cloud Ready**: Designed for deployment on Render, Heroku, or any Docker-compatible platform.

## Setup

### 1. Prerequisites

- Python 3.9+
- A Google Cloud Project with the following APIs enabled:
  - Google Docs API
  - Google Sheets API
  - Google Drive API
- `credentials.json` (Service Account or OAuth Client ID)

### 2. Installation

```bash
git clone https://github.com/StartUp-Agency-Modern-Nav/research-link-scraper.git
cd research-link-scraper
pip install -r requirements.txt
```

### 3. Configuration

Edit `config.py` to match your setup:

- `SPREADSHEET_ID`: ID of your Google Sheet.
- `INPUT_COLUMN_NAME`: Column header for links (e.g., "Post Topic Research Draft").
- `OUTPUT_COLUMN_NAME`: Column header for status (e.g., "Scraped Data").
- `SCOPED_DATA_FOLDER_ID`: Google Drive folder ID for output.

### 4. Running Locally

**First Run (Authentication)**:
Run the scraper locally once to generate `token.json` (OAuth token). This is interactive and requires a browser.

```bash
python main_service.py
```

_Follow the browser prompt to log in._

**Start Service**:

```bash
./start_service.bat
```

## Deployment (Render.com)

This project is ready for Render.

1. **Create Web Service**: Connect your GitHub repo.
2. **Settings**:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main_service.py`
3. **Environment Variables**:
   You must set these secrets in Render (do not commit `token.json` or `credentials.json`!):
   - `GOOGLE_CREDENTIALS_JSON`: Content of your `credentials.json`.
   - `GOOGLE_TOKEN_JSON`: Content of your `token.json` (generated locally).

   _Use the helper script `python generate_env_vars.py` locally to get these values easily._

## Project Structure

- `main_service.py`: Orchestrator that monitors the sheet and uploads files.
- `scraper.py`: Core logic for extracting content from various sources.
- `config.py`: Central configuration.
- `generate_env_vars.py`: Helper tool for cloud deployment.

## License

MIT
