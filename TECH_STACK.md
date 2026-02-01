# Research Link Scraper - Tech Stack

This document outlines the languages, frameworks, and libraries used in this project.

## 🌐 Core Languages

- **Python**: Backend logic, API, and scraping engine.
- **JavaScript (JSX)**: Frontend UI and interactivity.
- **HTML5 & CSS3**: Document structure and modern styling.

---

## ⚙️ Backend Technology Stack

The backend is built with a focus on high-performance asynchronous operations and robust data extraction.

- **FastAPI**: Modern, high-performance web framework for Python APIs.
- **Uvicorn**: ASGI web server implementation.
- **Scraping & Extraction Suite**:
  - `curl_cffi`: Advanced HTTP client (browser impersonation).
  - `trafilatura`: Web content and metadata extraction.
  - `BeautifulSoup4` & `lxml`: HTML/XML parsing.
  - `PyPDF`: PDF content extraction.
  - `aiohttp`: Asynchronous HTTP requests.
  - `youtube-transcript-api`: YouTube data extraction.
- **Google Workspace Integration**:
  - `google-api-python-client`: Official Google Docs API library.
  - `google-auth`: OAuth2 authentication for secure document access.

---

## 🎨 Frontend Technology Stack

The frontend is designed for a premium user experience and fast performance.

- **React (v19)**: Component-based UI library.
- **Vite**: Next-generation frontend build tool (extremely fast HMR).
- **ESLint**: Code quality and linting.

---

## 🛠️ Infrastructure & Utilities

- **Pydantic**: Data validation and settings management.
- **CORS Middleware**: Secure cross-origin resource sharing.
- **Token-based Auth**: Local storage of OAuth2 tokens for Google API access.
