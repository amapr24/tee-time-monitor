# ⛳️ Tee Time Monitor

A robust, automated monitoring system designed to track tee time availability at your favorite golf courses. This application scans booking platforms and sends real-time notifications so you never miss a chance to play.

## 🚀 Features

- **Automated Scanning:** Regularly checks for available slots based on your preferred dates, times, and player counts.
- **Multi-Course Support:** Monitor multiple golf courses simultaneously.
- **Instant Notifications:** Receive alerts via Discord, Email, or SMS (depending on configuration) the moment a matching slot opens up.
- **Headless Browser Integration:** Uses modern web scraping techniques to navigate dynamic booking sites.
- **Custom Filters:** Set criteria for "Earliest Start" and "Latest Start" times to ensure you only get notified for rounds that fit your schedule.

## 🛠 Tech Stack

- **Language:** Python 3.x
- **Automation:** Selenium / Playwright (for web interaction)
- **Task Scheduling:** `APScheduler` or `Cron` integration
- **Data Parsing:** BeautifulSoup4
- **Environment Management:** `python-dotenv`

## 📋 Prerequisites

Before running the application, ensure you have the following installed:
- [Python 3.8+](https://www.python.org/)
- [Google Chrome](https://www.google.com/chrome/) or [Firefox](https://www.mozilla.org/en-US/firefox/new/)
- [WebDriver](https://chromedriver.chromium.org/downloads) (matching your browser version)

## 🔧 Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/amapr24/tee-time-monitor.git
   cd tee-time-monitor
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configuration:**
   Create a `.env` file in the root directory and add your credentials/settings:
   ```env
   COURSE_URL=https://example-golf-course.com/booking
   NOTIFY_WEBHOOK=your_discord_webhook_url
   CHECK_INTERVAL=300  # Seconds between checks
   PLAYER_COUNT=4
   ```

## 🖥 Usage

To start the monitor, run:
```bash
python main.py
```

For background execution on a server:
```bash
nohup python main.py &
```

## ⚙️ How It Works

1. **Initialization:** The script loads your target courses and notification settings from the config.
2. **The Loop:** At every interval, it launches a headless browser instance.
3. **Scraping:** It navigates to the booking calendar, selects the target date, and parses available times.
4. **Validation:** It compares found slots against your "desired times" filter.
5. **Alerting:** If a new slot is found that wasn't present in the previous check, it triggers a notification.

## 🤝 Contributing

Contributions are welcome! If you'd like to add support for a new booking platform or improve the notification system:
1. Fork the Project.
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`).
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the Branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

## ⚖️ License

Distributed under the MIT License. See `LICENSE` for more information.

---
*Disclaimer: This tool is intended for personal use only. Ensure you comply with the Terms of Service of any website you monitor.*
