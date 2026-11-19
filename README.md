# Pre-Results Radar (NSE F&O)

Local dashboard on **http://localhost:8379**. Static page; it only changes when you press **Refresh** (sends Telegram)
or at the daily **08:30 IST** run, which also sends the Telegram message.

    ./run.sh                          # start the server (background)
    python3 radar/job.py              # build once from the terminal
    python3 radar/job.py --telegram   # build once and send Telegram

Put keys in `.env` (copy of `.env.example`): `OPENAI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Restart the server after editing `.env` (`pkill -f radar/server.py; ./run.sh`).

Data sources (all NSE public data): F&O lot list, board-meeting intimations + event calendar, integrated XBRL
results filings, corporate announcements (attached PDFs are read), daily price history. Sector names from niftyindices.com.
The first build takes ~15 minutes (downloads ~200 stocks' filings); later builds use the cache in `data/cache`.
