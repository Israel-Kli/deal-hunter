# Azure Deployment Plan — Standard B2ats v2 (2 vCPU, 1 GiB RAM)

## Architecture Decision: systemd + venv (no Docker)

At 1 GiB, Docker daemon + container overhead (~200 MB) leaves too little headroom.
We run directly on the host with:

- Python 3.11+ venv
- systemd service for process management (auto-restart, logging, health)
- SQLite on local disk (already in `data/`)

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
git clone <repo-url> .
```

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

Edit `configs/config.json`:

- `sources.yad2`: `true`
- `sources.onmap`: `true` (or `false` to save memory)
- `sources.ad`: `true`
- `schedule.poll_interval_minutes`: `60`
- `schedule.delay_between_requests_sec`: `3.0`
- `notifications.telegram_bot_token` / `chat_id`: your values
- `scoring.alert_threshold`: `7.0`

### 7. Secrets (env file)

```bash
cat > /opt/deal-hunter/.env << 'EOF'
TELEGRAM_BOT_TOKEN=<your-token>
TELEGRAM_CHAT_ID=<your-chat-id>
EOF
chmod 600 /opt/deal-hunter/.env
```

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

The dashboard `serve()` function defaults to `127.0.0.1`. We need it to listen on all interfaces.

**Option A (recommended)**: Add `dashboard_host` to config.

In `src/deal_hunter/config.py`, add to `Config`:
```python
dashboard_host: str = "0.0.0.0"
```

In `src/deal_hunter/cli.py`, update `cmd_dashboard`:
```python
def cmd_dashboard(args: argparse.Namespace) -> int:
    from deal_hunter.web.app import serve
    cfg = cfg_mod.load(args.config)
    serve(cfg, host=cfg.dashboard_host)
    return 0
```

**Option B (quick)**: Change the default in `app.py`:
```python
def serve(cfg: Config, host: str = "0.0.0.0") -> None:
```

### 12. Azure NSG Rule

- **Inbound rule**: allow TCP port `8081` from your IP (or `0.0.0.0/0` for anywhere)
- No other ports need to be open

---

## Phase 5: Verification

### 13. Smoke Test

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

### 14. Memory Monitoring (first 24h)

```bash
watch -n 5 'free -m && echo "---" && ps aux | grep deal-hunter | grep -v grep'
```

---

## Phase 6: Ongoing Ops

### 15. Log Rotation

Cap systemd journal to 100 MB:

```bash
# /etc/systemd/journald.conf
SystemMaxUse=100M
sudo systemctl restart systemd-journald
```

### 16. Code Updates

```bash
cd /opt/deal-hunter
git pull
.venv/bin/pip install -e .  # re-install if deps changed
sudo systemctl restart deal-hunter
```

### 17. DB Backup (optional, cron)

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
