# 📊 TradeVWAP — Flask + SQLite Signal Scanner

A live intraday signal scanner with a Flask dashboard hosted on Render.
**No redeploy on data updates** — Flask reads SQLite on every page request.

## Architecture

```
GitHub Actions (every ~2 min, 9AM-6PM EST)
        ↓
  scanner.py runs
        ↓
  writes signals.db
        ↓
  SCP pushes db → Render persistent disk
        ↓
Flask app reads db on every request → Live dashboard ✅
```

## Deployment — Step by Step

### 1. Push repo to GitHub
```bash
git init && git add . && git commit -m "init TradeVWAP Flask app"
git remote add origin https://github.com/YOUR_USERNAME/TradeVWAP.git
git push -u origin main
```

### 2. Deploy to Render

1. Go to https://render.com and sign up (free)
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub repo **TradeVWAP**
4. Render auto-detects `render.yaml` — click **"Apply"**
5. Your app deploys at: `https://tradevwap.onrender.com`

### 3. Get Render SSH credentials

Render provides SSH access to your running service:

1. Go to your service on Render → **"Shell"** tab
2. Note the SSH host and user shown (e.g. `ssh user@tradevwap.onrender.com`)
3. Generate an SSH key pair locally:
   ```bash
   ssh-keygen -t ed25519 -f render_key -N ""
   ```
4. Add `render_key.pub` contents to Render → **Settings → SSH Keys**

### 4. Add GitHub Secrets

Go to your GitHub repo → **Settings → Secrets → Actions**:

| Secret | Value |
|---|---|
| `EMAIL_SENDER` | your Gmail |
| `EMAIL_PASSWORD` | Gmail App Password |
| `EMAIL_RECEIVER` | alert destination email |
| `RENDER_SSH_KEY` | contents of `render_key` (private key) |
| `RENDER_SSH_HOST` | e.g. `tradevwap.onrender.com` |
| `RENDER_SSH_USER` | SSH user from Render shell tab |

### 5. Enable GitHub Actions

Go to **Actions tab** → enable workflows.
Scanner runs every ~2 minutes Mon–Fri 9AM–6PM EST.

## API Endpoints

| Endpoint | Description |
|---|---|
| `/` | Live dashboard |
| `/api/signals` | JSON signal data |
| `/api/stats` | JSON stats summary |
| `/health` | Health check |

Query params for `/api/signals`: `symbol`, `signal`, `strength`, `days`

Example: `/api/signals?signal=LONG&strength=3&days=7`

## Project Structure

```
TradeVWAP/
├── .github/workflows/scanner.yml   # GitHub Actions
├── templates/dashboard.html        # Flask HTML template
├── app.py                          # Flask web server
├── scanner.py                      # Signal scanner
├── requirements.txt
├── render.yaml                     # Render config
└── README.md
```

## Disclaimer
For informational purposes only. Not financial advice.
