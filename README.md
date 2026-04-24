# ⛳️ Tee Time Monitor

A high-performance monitoring system for Miami-area golf courses. This tool automates the tedious process of refreshing booking pages by scraping available tee times and delivering real-time alerts via Pushover and Email.

## 🚀 Overview

Booking a weekend tee time in Miami is often a race against time. **Tee Time Monitor** solves this by checking multiple booking platforms (CPS Golf, Chronogolf, and WebTrac) every 15 minutes. When a new slot opens within your preferred window, you get a notification instantly.

### Key Features
- **Multi-Platform Support:** Custom scrapers for CPS Golf, Chronogolf, and WebTrac.


  | Course | Booking platform |
  |---|---|
  | Miami Beach | Chronogolf |
  | Miami Shores | Chronogolf |
  | Normandy Shores | Chronogolf |
  | Miami Lakes | cpsgolf |
  | Plantation Preserve | WebTrac |


- **Smart Window Filtering:** Automatically calculates the "sunset cutoff" (using the `astral` library) to filter out times that are too late to play.
- **Real-Time Notifications:** Immediate alerts via **Pushover** (push notifications) and **SMTP Email**.
- **Interactive Dashboard:** A lightweight `index.html` generated after every run featuring:
    - **Color-Coded Slots:** Visual indicators for Early, Midday, Afternoon, and Twilight slots.
    - **"New" Highlighting:** Recently discovered slots are pulsed in gold.
    - **Day Filtering:** Quickly toggle between Friday, Saturday, and Sunday.
    - **Dark Mode:** Full theme support for late-night monitoring.
- **Efficient Caching:** State is persisted in JSON cache files to prevent duplicate notifications.

---

## 🛠 Setup & Installation

### 1. Clone the Repository
```bash
git clone https://github.com/amapr24/tee-time-monitor.git
cd tee-time-monitor
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configuration
The monitor uses environment variables for sensitive credentials. Create a `.env` file or export the following variables in your environment:

| Variable | Description |
| :--- | :--- |
| `PUSHOVER_USER` | Your Pushover user key |
| `PUSHOVER_TOKEN` | Your Pushover API token |
| `EMAIL_SENDER` | The email address used to send alerts |
| `EMAIL_PASSWORD` | App-specific password for the sender email |
| `EMAIL_TO` | Recipient email(s) (comma-separated) |
| `SMTP_SERVER` | SMTP server address (e.g., `smtp.gmail.com`) |
| `SMTP_PORT` | SMTP port (e.g., `587`) |

---

## ⚙️ How it Works

### The Scraper
The system uses **Playwright** in headless mode to mimic human browsing behavior (including random delays and custom user-agents) to avoid bot detection. It navigates the calendar systems of various courses and extracts raw time-slot data.

### The "Sunset Logic"
Instead of a hard-coded cutoff time, the monitor uses the **Astral** library to calculate the actual sunset time in Miami for the specific target date. It sets the `tee_time_max` to **4 hours before sunset**, ensuring you only get alerted for rounds that can be finished in daylight.

### The Dashboard
Every time the script runs, it regenerates an `index.html` file. This file can be hosted on GitHub Pages or any static host to provide a real-time visual overview of course availability across the weekend.

---

## 📅 Automation (GitHub Actions)

To keep this running 24/7, it is recommended to use a GitHub Action with a `cron` schedule.

**Example Workflow Schedule:**
```yaml
on:
  schedule:
    - cron: '*/15 * * * *' # Runs every 15 minutes
```

## 🛠 Adding New Courses
To add a new course, update the `COURSES` list in `tee_time_monitor.py` with the course's metadata, booking URL, and the appropriate scraper type (`cpsgolf`, `chronogolf`, or `webtrac`).

---

## 📜 License
MIT License. See `LICENSE` for details.

