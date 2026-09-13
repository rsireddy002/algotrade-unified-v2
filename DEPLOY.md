# Deployment Runbook — algotrade-unified

Deploys the dashboard and feed listener as persistent systemd services on
a brand-new EC2 instance, matching the exact pattern already proven in
production for breakout-scanner-streamlit (same key pair reuse, same
service structure, same verification steps).

**Not tested against a real EC2 instance in this build** — I have no AWS
access from this sandbox. What IS verified: both `.service` files pass
`systemd-analyze verify` with zero syntax/structural errors (the only
complaint is that binary paths don't exist yet, which is expected until
step 4 below creates them). The commands below mirror your own
successful breakout-scanner-streamlit deployment steps.

**Honest scope note**: the feed listener service just logs decoded ticks
to the systemd journal — it does NOT yet feed the dashboard's hero cards
or the CVD tracker. That integration (a background thread + shared state
Streamlit can poll) was explicitly deferred in Phase 5 as its own
reliability problem, not attempted here. Running both services on the
same box is a first step toward that, not the integration itself.

## 1. Launch the EC2 instance

- Region: ap-south-1 (Mumbai), matching your existing instances
- Instance type: t3.micro (same as your others)
- AMI: Ubuntu (same version as your existing boxes)
- Key pair: reuse `fno-listener-key` (same as your other instances) or
  create a new one — either works, this is a fresh box either way
- Security group: inbound rules for SSH (22) and Custom TCP (8501),
  source Anywhere — same pattern as breakout-scanner-streamlit's
  `launch-wizard-3` group

## 2. Create the GitHub repo

On your PC, in the `algotrade-unified` folder:

```powershell
cd "C:\Users\MY PC\Desktop\algotrade-unified\algotrade-unified"
$env:PATH += ";C:\Program Files\Git\cmd"
git init
git add .
git commit -m "Initial commit: unified F&O trading platform"
git branch -M main
git remote add origin https://github.com/rsireddy002/algotrade-unified.git
git push -u origin main
```

(Create the empty repo on GitHub first if you haven't — same account,
`rsireddy002`, matching your other repos.)

## 3. SSH in and clone

```bash
ssh -i "$env:USERPROFILE\Downloads\fno-listener-key.pem" ubuntu@<NEW_INSTANCE_IP>
```

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
git clone https://github.com/rsireddy002/algotrade-unified.git
cd algotrade-unified
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Set up `.env`

```bash
cp .env.example .env
nano .env
```

Paste in your real `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, and `ANTHROPIC_API_KEY` — same values as your local
`.env`. If nano gives you the paste trouble it's given you before, use
the heredoc method instead:

```bash
cat > .env << 'EOF'
UPSTOX_ACCESS_TOKEN=your_actual_token_here
TELEGRAM_BOT_TOKEN=your_actual_bot_token_here
TELEGRAM_CHAT_ID=your_actual_chat_id_here
ANTHROPIC_API_KEY=your_actual_key_here
EOF
```

Verify it landed correctly:

```bash
cat .env
```

## 5. Install the systemd services

```bash
sudo cp deploy/algotrade-dashboard.service /etc/systemd/system/
sudo cp deploy/algotrade-feedlistener.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable algotrade-dashboard algotrade-feedlistener
sudo systemctl start algotrade-dashboard algotrade-feedlistener
```

## 6. Verify

```bash
sudo systemctl status algotrade-dashboard
sudo systemctl status algotrade-feedlistener
```

Both should show `active (running)`. Then:

- Visit `http://<NEW_INSTANCE_IP>:8501` in your browser for the dashboard
- Check the feed listener is actually receiving ticks (during market hours):

```bash
sudo journalctl -u algotrade-feedlistener -f
```

You should see the same kind of output as the local test: `Connected to
feed`, then lines like `NSE_FO|68407: LTP=... vol_today=...`. If you see
the "no feed message received in over 30s" warning instead, that's the
watchdog catching the known "connected but zero ticks" failure mode —
not a crash, but worth investigating (check the instrument keys haven't
gone stale after a contract rollover).

## Notes

- `data_cache/` (F&O universe cache, dashboard signal/trade logs) is
  gitignored — it'll be created fresh on the server, separate from your
  local copy. This means paper trades and dedup state on the server are
  independent of your local runs.
- Rerunning `git pull` on the server after future local changes: same
  pattern as your other repos — `git add . && git commit && git push`
  locally, `git pull` on the server, then `sudo systemctl restart
  algotrade-dashboard algotrade-feedlistener` to pick up the changes.
