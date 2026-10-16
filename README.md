# AppsFlyer Master / A2Z Game Master Bot 🎮

A powerful, asynchronous Telegram bot designed to manage games, schedule events, and dispatch Server-to-Server (S2S) in-app events and in-app purchase receipts directly to the **AppsFlyer S2S In-App Event API**. Backed by **Supabase** for secure user authentication and persistent cloud storage.

---

## 📌 Project Overview

* **Repository:** [alexmurphy3526-ship-it/a2z-game-master](https://github.com/alexmurphy3526-ship-it/a2z-game-master)
* **Platforms Supported:** Android (GAID / Advertising ID) & iOS (IDFA)
* **Primary Integration:** AppsFlyer S2S In-App Event API (`api2.appsflyer.com/inappevent/{app_id}`)
* **Backend Database & Auth:** Supabase (PostgreSQL + GoTrue Auth)

---

## 🚀 Key Features

### 1. 📤 AppsFlyer S2S Event Dispatcher
- Dispatches Server-to-Server (S2S) in-app event tracking payloads directly to AppsFlyer without running the game client.
- Supports platform-specific timestamping:
  - **Android:** UTC ISO 8601 formatting with `advertising_id` (GAID).
  - **iOS:** Microsecond-precision timestamps with `idfa`.

### 2. 💳 Simulated In-App Purchases (IAP)
- Generates simulated purchase events with revenue (`af_revenue`), currency code (`af_currency`), and product IDs.
- For Android, automatically formats authentic Google Play receipt structures (`orderId` with `GPA.XXXX-XXXX-XXXX-XXXXX`, `purchaseToken`, and `receipt_data`).

### 3. 🎯 Game & Task Management
- Tracks assigned games, milestone levels, and parameterized tasks (e.g. `@`-based levels).
- Device identifier management: Add, view, and delete AppsFlyer UIDs, GAIDs, and IDFAs up to configured slot limits per game.

### 4. ⏰ Automated Event Scheduler
- Schedule task completion events and purchase events for any future date and time.
- Integrated Bangladesh Standard Time (BDT / UTC+6) timezone handling.
- Robust background scheduler running every 30 seconds with automatic retry on failures (`MAX_SCHED_RETRIES = 3`).

### 5. 👥 User Authentication & Admin Panel
- **Security & Device Binding:** Single Telegram account locking (`telegram_id`) per user to prevent unauthorized account sharing.
- **Session Auto-Refresh:** Automatically refreshes expiring Supabase access tokens in the background to ensure uninterrupted operation.
- **Admin Management Suite:**
  - Create new user accounts directly through Telegram.
  - Ban / unban accounts.
  - Copy game packages and configurations across users.
  - Set custom UID limits and validation expiration dates.
  - Global broadcast messaging system to all users.

### 6. 🌐 Guest & Explore Mode
- Visitors can explore available games, supported platforms, and plans before logging in.

---

## 💻 Languages & Tech Stack

| Technology | Purpose |
| :--- | :--- |
| **Python 3** | Core programming language |
| **python-telegram-bot (v21.7)** | Asynchronous Telegram Bot API framework |
| **requests (>=2.31.0)** | HTTP client for Supabase REST endpoints & AppsFlyer API |
| **certifi (>=2024.2.2)** | SSL certificate validation |
| **Supabase** | Cloud PostgreSQL database & GoTrue Auth (Admin & User level) |
| **asyncio** | Background job scheduling and async task execution |

---

## 💰 Pricing & Subscription Plans

Game access and bot licensing options:

| Plan Validity | Estimated Price (USD) |
| :--- | :--- |
| **15 Days** | ~$15 |
| **1 Month** | ~$20 |
| **2 Months** | ~$30 |
| **3 Months** | ~$40 |
| **6 Months** | ~$60 |

> *Note: Pricing can vary based on single-platform (Android or iOS) versus dual-platform bundles.*  
> For licensing, custom game packages, or purchasing access, contact the admin on Telegram: **[@whoiam31](https://t.me/whoiam31)**.

---

## 🛠️ Setup & Installation

### 1. Clone the Repository
```bash
git clone https://github.com/alexmurphy3526-ship-it/a2z-game-master.git
cd a2z-game-master
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment Variables
Set the required secrets in your host environment or `.env` file before launching:

```bash
BOT_TOKEN=your_telegram_bot_token
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your_supabase_anon_key
SUPABASE_SERVICE_KEY=your_supabase_service_role_key
```

### 4. Run the Bot
```bash
python bot.py
```

---

## 📞 Support & Inquiries

For inquiries, support, or new game additions, contact:
* **Telegram:** [@whoiam31](https://t.me/whoiam31)
