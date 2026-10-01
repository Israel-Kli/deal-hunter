# Deal Hunter Deployment Plan — systemd + venv (Azure origin, now AWS EC2)

## Architecture Decision: systemd + venv (no Docker)

At 1 GiB, Docker daemon + container overhead (~200 MB) leaves too little headroom.
We run directly on the host with:

- Python 3.11+ venv
- systemd service for process management (auto-restart, logging, health)
- SQLite on local disk (already in `data/`)

---

## Current Production Host (AWS EC2, replaces the Azure VM)

The Azure VM described below stopped working and was replaced by an AWS EC2
instance (Amazon Linux 2023, 2 vCPU, 2 GiB RAM + 2 GiB swap, il-central-1):

- **SSH**: `ssh aws-ec2` → `51.85.56.244` (user `ec2-user`, key-based)
- **Path**: `/opt/deal-hunter`, dedicated `deal-hunter` user
- **Python**: 3.14.7 prebuilt at `/opt/py314`; the venv is built from it
  (`/opt/py314/bin/python3 -m venv .venv`). Amazon Linux ships only Python 3.9,
  too old for the pinned pandas/numpy — the Ubuntu deadsnakes steps below are
  the Azure equivalent
- **Packages**: `dnf`, not `apt`; nothing extra is needed on this box — the
  venv interpreter and pure-wheel deps cover everything
- **Firewall**: EC2 security group (not an Azure NSG) — allow inbound TCP 8081
- **Repo**: public, so `git clone https://github.com/Israel-Kli/deal-hunter.git`
  works without a deploy key

---

## Phase 1: Azure VM Prep

### 1. OS

Ubuntu 24.04 LTS (or 22.04 if already provisioned).

### 2. System Packages

```bash
sudo apt update && sudo apt install -y \
  python3.12-venv python3.12-dev gcc \
  libcurl4-openssl-dev libffi-dev pkg-config git
```

### 3. Swap File (critical at 1 GiB)

Prevents OOM during `pip install` and scraping spikes.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Prefer RAM but use swap when needed
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
```

---

## Phase 2: Code + venv Setup

### 4. Clone the Repo

```bash
mkdir -p /opt/deal-hunter
cd /opt/deal-hunter
git clone https://github.com/Israel-Kli/deal-hunter.git .
```

The repo is public, so HTTPS clone works without credentials. If cloning as the
dedicated user instead of root, create the user first (step 8) and run
`sudo -u deal-hunter git clone https://github.com/Israel-Kli/deal-hunter.git .`.

### 5. Create venv + Install

```bash
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

### 6. Configuration

```bash
cp configs/config.example.json configs/config.json
```

Edit `configs/config.json` (gitignored, not in the repo):

- `cities` — one entry per city: `name`, `slug` + `yad2_id`/`yad2_area`/`yad2_region`
  (Yad2 feed), `onmap_slug`, `hebrew_name`. Optional Yad2 gw geo-targeting:
  `yad2_region_id`, `yad2_bbox`, `yad2_tiles`
- `sources` — per-scraper toggles: `yad2`, `madlan`, `onmap`, `ad`, `reariel`,
  `spectra`, `nadlanh`, `simplestate`, `komo`
- `comps` — `yad2_deals`, `nadlan_gov` (both currently non-functional from cloud IPs)
- `schedule.poll_interval_minutes` / `delay_between_requests_sec` / `max_pages`
- `notifications` — `telegram_bot_token` / `chat_id` (leave empty to disable)
- `scoring.alert_threshold` — `7.0`
- `gsheets` — `enabled`, `sheet_id`, `credentials_path`, `tab_name`,
  `archive_tab_name` (needs a service-account key, step 7)
- `ai_mapper` — `enabled` (Gemini enrichment; expects the key in `GEMINI_API_KEY`)
- `freebies` — agora.co.il keyword watches (`enabled`, `interval_minutes`, `watches`)
- `dashboard_host` — `"0.0.0.0"` for external dashboard access

### 7. Secrets (env file + Google key)

```bash
cat > /opt/deal-hunter/.env << 'EOF'
TELEGRAM_BOT_TOKEN=<your-token>
TELEGRAM_CHAT_ID=<your-chat-id>
EOF
chmod 600 /opt/deal-hunter/.env
```

If `gsheets.enabled` is `true`, also upload the service-account JSON to the repo
root as `credentials.json` (the `credentials_path` default), chown it to the app
user and `chmod 600`. Create the key under Google Cloud → IAM → Service Accounts
and share the target spreadsheet with the service-account email (Editor).

---

## Phase 3: systemd Service

### 8. Create Dedicated User

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/deal-hunter deal-hunter
sudo chown -R deal-hunter:deal-hunter /opt/deal-hunter
```

### 9. Create Service File

`/etc/systemd/system/deal-hunter.service`:

```ini
[Unit]
Description=Deal Hunter - Multi-source real estate monitor
After=network.target

[Service]
Type=simple
User=deal-hunter
Group=deal-hunter
WorkingDirectory=/opt/deal-hunter
Environment=PATH=/opt/deal-hunter/.venv/bin:/usr/bin
EnvironmentFile=/opt/deal-hunter/.env
ExecStart=/opt/deal-hunter/.venv/bin/deal-hunter run
Restart=on-failure
RestartSec=30

# Memory safety: hard limit at 800 MB to leave 200 MB for OS
MemoryMax=800M
MemorySwapMax=2G

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=deal-hunter

[Install]
WantedBy=multi-user.target
```

### 10. Enable + Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable deal-hunter
sudo systemctl start deal-hunter
sudo systemctl status deal-hunter
journalctl -u deal-hunter -f --no-pager
```

---

## Phase 4: Dashboard Exposure

### 11. Bind to 0.0.0.0

`dashboard_host` already exists in `src/deal_hunter/config.py` (default
`"127.0.0.1"`) and `cmd_dashboard` in `cli.py` already passes it to `serve()`.
No code changes needed — just set it in `configs/config.json`:

```json
"dashboard_host": "0.0.0.0",
"dashboard_port": 8081
```

### 12. Dashboard systemd Unit

The scraper service (step 9) does not serve the dashboard; that is a separate
process. Create `/etc/systemd/system/deal-hunter-dashboard.service`:

```ini
[Unit]
Description=Deal Hunter Dashboard (port 8081)
After=network.target

[Service]
Type=simple
User=deal-hunter
Group=deal-hunter
WorkingDirectory=/opt/deal-hunter
Environment=PATH=/opt/deal-hunter/.venv/bin:/usr/bin
EnvironmentFile=/opt/deal-hunter/.env
ExecStart=/opt/deal-hunter/.venv/bin/deal-hunter dashboard
Restart=on-failure
RestartSec=10

MemoryMax=300M
MemorySwapMax=1G

StandardOutput=journal
StandardError=journal
SyslogIdentifier=deal-hunter-dashboard

[Install]
WantedBy=multi-user.target
```

Enable + start both units:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now deal-hunter deal-hunter-dashboard
```

### 13. Firewall Rule (Azure NSG / EC2 security group)

- **Inbound rule**: allow TCP port `8081` from your IP (or `0.0.0.0/0` for anywhere)
- No other ports need to be open

---

## Phase 5: Verification

### 14. Smoke Test

```bash
# Check service is running
sudo systemctl is-active deal-hunter

# Check it's listening
ss -tlnp | grep 8081

# Hit the API
curl http://<vm-public-ip>:8081/healthz
curl http://<vm-public-ip>:8081/api/listings | python3 -m json.tool | head -20

# Check logs for scan results
journalctl -u deal-hunter --since "5 min ago" | grep "scan "
```

### 15. Memory Monitoring (first 24h)

```bash
watch -n 5 'free -m && echo "---" && ps aux | grep deal-hunter | grep -v grep'
```

---

## Phase 6: Ongoing Ops

### 16. Log Rotation

Cap systemd journal to 100 MB:

```bash
# /etc/systemd/journald.conf
SystemMaxUse=100M
sudo systemctl restart systemd-journald
```

### 17. Code Updates

```bash
cd /opt/deal-hunter
git pull
.venv/bin/pip install -e .  # re-install if deps changed
sudo systemctl restart deal-hunter
```

### 18. DB Backup (optional, cron)

```bash
# /etc/cron.d/deal-hunter-backup
0 3 * * * root cp /opt/deal-hunter/data/deal-hunter.db /opt/deal-hunter/data/backup-$(date +\%F).db && find /opt/deal-hunter/data -name 'backup-*.db' -mtime +7 -delete
```

---

## Memory Budget Estimate

| Component | RAM |
|-----------|-----|
| OS + systemd | ~150 MB |
| Python process (idle) | ~80 MB |
| Python process (scraping peak) | ~200–300 MB |
| Swap (safety net) | 2 GB |
| **Headroom** | ~400–500 MB free |

Should be comfortable. The `MemoryMax=800M` cgroup limit prevents runaways.

---

## Troubleshooting

### OOM Kill

```bash
# Check if the service was killed
journalctl -u deal-hunter | grep -i "oom\|killed"

# If so, reduce max_pages or disable a source in config.json
```

### Service Won't Start

```bash
# Check detailed logs
journalctl -u deal-hunter -n 100 --no-pager

# Test manually
cd /opt/deal-hunter
.venv/bin/deal-hunter run --once --max-items 5
```

### Dashboard Not Reachable

```bash
# Check it's listening
ss -tlnp | grep 8081

# Check NSG rule in Azure Portal
# Check if firewall is blocking
sudo ufw status
```

### High Memory Usage

```bash
# Check per-process memory
ps aux --sort=-%mem | head -10

# If deal-hunter is using too much, reduce sources or max_pages
```
