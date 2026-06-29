# PostgreSQL Sync API deployment guide

This guide deploys the current FastAPI application in `main.py` between two PostgreSQL databases:

```text
Source PostgreSQL  ── TLS / firewall ──>  Sync API VM  ── private network preferred ──>  Target PostgreSQL
```

The API reads selected tables from the source, discovers their foreign-key parents, then upserts parent tables before child tables in the target.

## Document hierarchy

1. Architecture overview
2. Recommended hosting model
3. Network and security rules
4. Config precedence
5. Local development run
6. VM setup
7. System service
8. Production updates with Git
9. Production config updates
10. Recommended auto deployment
11. Subdomain and HTTPS
12. First sync
13. Normal incremental sync
14. Operating rules
15. Known current limitations

## Recommended hosting model

Run one API instance on a small Linux VM in the same cloud region and private network as the **target** PostgreSQL database.

This is the right model for the current application because it uses:

- `sync_meta.db` for cursor state and run history;
- an OS file lock to stop overlapping syncs; and
- a single-process deployment model.

Do not run multiple replicas, serverless instances, or separate containers against the same configuration yet. Each instance would have its own SQLite state and lock, which can lead to duplicate or overlapping syncs.

Use the cloud provider already hosting the target database when possible:

- Target on AWS: small EC2 VM in the target VPC and region.
- Target on DigitalOcean: small Droplet in the target VPC and region.
- Target on another provider: use its equivalent small VM with persistent disk.

Assign the API VM a stable outbound IP. The source PostgreSQL firewall should allow port `5432` only from that IP.

## Network and security rules

Apply these rules before deploying:

| Component | Required access |
|---|---|
| Source PostgreSQL | Accept `5432` only from the API VM's fixed outbound IP; require TLS for public-network traffic. |
| Target PostgreSQL | Prefer private-network access only; accept `5432` only from the API VM/security group. |
| API VM | Accept `443` only from the calling application, VPN, or trusted office IPs. Do not expose PostgreSQL. |
| API process | Read database credentials and `SYNC_API_KEY` from `.env`, never from a production request body. |

Create dedicated database roles:

- Source role: `CONNECT`, schema `USAGE`, and `SELECT` only.
- Target role: `CONNECT`, schema `USAGE`, `SELECT`, `INSERT`, and `UPDATE` only.
- Do not use a PostgreSQL superuser for either connection.

Keep `ALLOWED_DB_HOSTS` set whenever request-level database overrides are enabled. In production, prefer omitting `source_db` and `local_db` from requests entirely and using the server-side `.env` configuration.

## Config precedence

The app has two DB config sources:

- server-side `.env`
- per-request `source_db` / `local_db` overrides

The precedence is simple:

- if a request omits `source_db` or `local_db`, the server uses `.env`
- if a request includes either block, that block replaces the `.env` values for that request
- if both are missing, the app cannot connect

Production recommendation:

- keep real credentials only in `.env`
- do not send DB configs in the request body unless you are intentionally doing an override
- keep `ALLOWED_DB_HOSTS` populated if overrides are enabled at all

## Local development run

Use this when you want to run the FastAPI app from your laptop or workstation.

```bash
cd /Users/vanny/development/sync_data_psql
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
```

Edit `.env` and fill in the database and API key values. For local development, keep the API's own SQLite state in the project folder:

```dotenv
SYNC_META_DB_PATH=sync_meta.db
SYNC_LOCK_FILE=/tmp/db_sync_api.lock
SYNC_API_KEY=replace-with-a-local-test-key
```

Start the API:

```bash
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Open the generated API docs:

```text
http://127.0.0.1:8000/docs
```

Run a dry-run request before copying rows:

```bash
curl --fail-with-body http://127.0.0.1:8000/api/sync \
  -H "X-API-Key: replace-with-a-local-test-key" \
  -H "Content-Type: application/json" \
  --data '{"tables":["custom_form_entries"],"dry_run":true}'
```

Run a normal incremental sync:

```bash
curl --fail-with-body http://127.0.0.1:8000/api/sync \
  -H "X-API-Key: replace-with-a-local-test-key" \
  -H "Content-Type: application/json" \
  --data '{"tables":["custom_form_entries"]}'
```

Important local notes:

- `localhost` in a request means the machine running the API. If the API runs on your laptop, local DB hosts must be reachable from your laptop.
- Supabase or other hosted PostgreSQL endpoints can be used from local development when the credentials, firewall rules, and `sslmode=require` are correct.
- Do not commit `.env`, `sync_meta.db`, or real database credentials.

## VM setup (Ubuntu example)

Run these commands as a sudo-capable administrator.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
sudo mkdir -p /opt/db-sync-api /var/lib/db_sync_api
sudo chown -R ubuntu:ubuntu /opt/db-sync-api /var/lib/db_sync_api
```

Copy this project to `/opt/db-sync-api`, then install its dependencies:

```bash
cd /opt/db-sync-api
sudo -u ubuntu python3 -m venv .venv
sudo -u ubuntu .venv/bin/pip install -r requirements.txt
sudo -u ubuntu cp env.example .env
sudo chmod 600 .env
sudo chown ubuntu:ubuntu .env
```

Edit `.env` and set real values. Keep the API state on persistent disk:

```dotenv
SOURCE_DB_HOST=source-db.example.internal
SOURCE_DB_PORT=5432
SOURCE_DB_NAME=production_db
SOURCE_DB_USER=sync_reader
SOURCE_DB_PASSWORD=replace-with-a-secret
SOURCE_DB_SSLMODE=require

LOCAL_DB_HOST=target-db.example.internal
LOCAL_DB_PORT=5432
LOCAL_DB_NAME=mirror_db
LOCAL_DB_USER=sync_writer
LOCAL_DB_PASSWORD=replace-with-a-secret
LOCAL_DB_SSLMODE=require

SYNC_META_DB_PATH=/var/lib/db_sync_api/sync_meta.db
SYNC_LOCK_FILE=/var/lib/db_sync_api/db_sync_api.lock
SYNC_API_KEY=replace-with-a-long-random-secret
ALLOWED_DB_HOSTS=source-db.example.internal,target-db.example.internal
```

For a local development socket connection, PostgreSQL commonly uses `/tmp` and no TLS:

```dotenv
SOURCE_DB_HOST=/tmp
SOURCE_DB_SSLMODE=disable
```

Use that only when the API and PostgreSQL run on the same machine and socket authentication is configured.

## Run as a system service

Create `/etc/systemd/system/db-sync-api.service`:

```ini
[Unit]
Description=PostgreSQL Sync API
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/db-sync-api
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/db-sync-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Start and inspect it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now db-sync-api
sudo systemctl status db-sync-api
sudo journalctl -u db-sync-api -f
```

Keep `--workers 1`. The current file lock and SQLite state are designed for one process.

Put Caddy, Nginx, or a cloud load balancer in front of port `8000` to terminate HTTPS. Do not expose `8000` directly to the internet.

## Production updates with Git

Use this for normal production updates when the VM already has the repo checked out at `/opt/db-sync-api`.

SSH to the VM:

```bash
ssh ubuntu@your-api-vm
cd /opt/db-sync-api
```

Back up the production-only files before changing code:

```bash
sudo cp .env .env.backup.$(date +%Y%m%d-%H%M%S)
sudo cp /var/lib/db_sync_api/sync_meta.db /var/lib/db_sync_api/sync_meta.db.backup.$(date +%Y%m%d-%H%M%S)
```

Pull the latest code, update dependencies, and validate the Python entrypoint:

```bash
sudo -u ubuntu git pull --ff-only
sudo -u ubuntu .venv/bin/pip install -r requirements.txt
sudo -u ubuntu .venv/bin/python -m py_compile main.py
```

Restart and verify the service:

```bash
sudo systemctl restart db-sync-api
sudo systemctl status db-sync-api
curl --fail-with-body http://127.0.0.1:8000/docs
```

If the service fails, inspect logs:

```bash
sudo journalctl -u db-sync-api -f
```

Do not overwrite `.env` during updates. Do not delete `/var/lib/db_sync_api/sync_meta.db` during normal updates; it stores sync cursors and run history.

Nginx or Caddy does not need a reload for normal Python code changes. Reload the reverse proxy only after changing its config.

## Production config updates

Keep production `.env` on the VM. Do not commit it to Git and do not deploy it from GitHub Actions unless you later add a proper secret-management flow.

Use this process when changing database hosts, credentials, SSL mode, the API key, allowed hosts, or sync state paths:

```bash
ssh ubuntu@your-api-vm
cd /opt/db-sync-api

sudo cp .env .env.backup.$(date +%Y%m%d-%H%M%S)
nano .env

sudo systemctl restart db-sync-api
sudo systemctl status db-sync-api
curl --fail-with-body http://127.0.0.1:8000/docs
```

If the service fails after the edit, inspect logs:

```bash
sudo journalctl -u db-sync-api -f
```

Common `.env` changes that require a restart:

- `SOURCE_DB_HOST`, `SOURCE_DB_PORT`, `SOURCE_DB_NAME`, `SOURCE_DB_USER`, `SOURCE_DB_PASSWORD`, `SOURCE_DB_SSLMODE`
- `LOCAL_DB_HOST`, `LOCAL_DB_PORT`, `LOCAL_DB_NAME`, `LOCAL_DB_USER`, `LOCAL_DB_PASSWORD`, `LOCAL_DB_SSLMODE`
- `SYNC_API_KEY`
- `ALLOWED_DB_HOSTS`
- `SYNC_META_DB_PATH`
- `SYNC_LOCK_FILE`

Recommended production config rules:

- keep code deployment automated, but keep `.env` edits manual on the VM
- keep `SYNC_META_DB_PATH=/var/lib/db_sync_api/sync_meta.db`
- keep `SYNC_LOCK_FILE=/var/lib/db_sync_api/db_sync_api.lock`
- keep `ALLOWED_DB_HOSTS` populated when request-level DB overrides are enabled
- back up `.env` before every config edit
- back up `/var/lib/db_sync_api/sync_meta.db` before changing source or target database identity

Changing database host, name, or user can affect sync cursor scope. If you point the API at a new source or target database, run a `dry_run` first. For a new or rebuilt target database, run the first real sync with `full_resync`.

## Recommended auto deployment

Recommended approach: use GitHub Actions to SSH into the single production VM, run `git pull --ff-only`, install dependencies, compile-check `main.py`, restart systemd, and verify `/docs` locally.

This matches the current single-instance design. Avoid auto-deploying to multiple servers until cursor state and locking are moved out of local files.

### One-time VM setup

Create an SSH key for deployment on your local machine:

```bash
ssh-keygen -t ed25519 -C "db-sync-api-deploy" -f ~/.ssh/db_sync_api_deploy
```

Add the public key to the VM:

```bash
ssh-copy-id -i ~/.ssh/db_sync_api_deploy.pub ubuntu@your-api-vm
```

Make sure the `ubuntu` user can restart only this service without a password:

```bash
sudo visudo
```

Add this line:

```text
ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart db-sync-api
```

If your server uses `/usr/bin/systemctl`, confirm the path first:

```bash
command -v systemctl
```

### GitHub repository secrets

In GitHub, open the repo settings and add these Actions secrets:

```text
PROD_HOST=your-api-vm-public-ip-or-hostname
PROD_USER=ubuntu
PROD_SSH_KEY=contents of ~/.ssh/db_sync_api_deploy
PROD_APP_DIR=/opt/db-sync-api
```

### GitHub Actions workflow

Create `.github/workflows/deploy-production.yml`:

```yaml
name: Deploy production

on:
  push:
    branches:
      - main
  workflow_dispatch:

concurrency:
  group: production-deploy
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy over SSH
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.PROD_HOST }}
          username: ${{ secrets.PROD_USER }}
          key: ${{ secrets.PROD_SSH_KEY }}
          script_stop: true
          script: |
            set -euo pipefail
            cd "${{ secrets.PROD_APP_DIR }}"

            cp .env ".env.backup.$(date +%Y%m%d-%H%M%S)"
            if [ -f /var/lib/db_sync_api/sync_meta.db ]; then
              cp /var/lib/db_sync_api/sync_meta.db "/var/lib/db_sync_api/sync_meta.db.backup.$(date +%Y%m%d-%H%M%S)"
            fi

            git fetch origin main
            git merge --ff-only origin/main

            .venv/bin/pip install -r requirements.txt
            .venv/bin/python -m py_compile main.py

            sudo systemctl restart db-sync-api
            systemctl status db-sync-api --no-pager
            curl --fail-with-body http://127.0.0.1:8000/docs >/dev/null
```

Use `workflow_dispatch` for manual deploys if you do not want every push to `main` to deploy. For that mode, remove the `push` block.

The workflow intentionally restarts only one VM. It also keeps `.env` and `sync_meta.db` on the VM instead of replacing them from Git.

## EC2 + Nginx path

If you are using EC2, the usual layout is:

```text
Internet -> Nginx on EC2 :80/:443 -> Uvicorn on 127.0.0.1:8000
```

Use this when you already see the Nginx welcome page on the domain root. That means Nginx is running, but the request is not yet being proxied to FastAPI.

### Nginx config

Create a site file such as `/etc/nginx/sites-available/db-sync-api`:

```nginx
server {
    listen 80;
    server_name db-sync-api.vanny.monster;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then enable it and remove the default site:

```bash
sudo ln -s /etc/nginx/sites-available/db-sync-api /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

If `http://db-sync-api.vanny.monster/` still shows the Nginx welcome page, check:

- `server_name` matches the domain exactly
- the default Nginx site is disabled
- `proxy_pass` points to `127.0.0.1:8000`
- `curl http://127.0.0.1:8000/docs` works on the EC2 instance

### Verify the app locally

Run these from the EC2 instance:

```bash
curl http://127.0.0.1:8000/docs
curl http://127.0.0.1:8000/openapi.json
```

If those work locally but the public domain does not, the issue is Nginx or DNS, not FastAPI.

### Add HTTPS with Certbot

After the HTTP proxy works, install Certbot and issue a certificate:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d db-sync-api.vanny.monster
```

Certbot updates the Nginx config and adds the TLS server block. After that, test:

```bash
curl -I https://db-sync-api.vanny.monster/docs
```

If renewal is enabled through the package, keep port `80` reachable for certificate renewal.

### If you use Cloudflare

Use Cloudflare only for the API domain, not for PostgreSQL.

Recommended settings:

- DNS record: proxy enabled only after the origin HTTPS certificate is working
- SSL/TLS mode: `Full (strict)`
- Do not use `Flexible`

If Cloudflare proxies the API, the request path becomes:

```text
Browser -> Cloudflare -> Nginx on EC2 -> Uvicorn
```

Keep the origin server still serving valid HTTPS. Cloudflare is an edge proxy, not a replacement for origin TLS.

## Subdomain and HTTPS

Use a dedicated subdomain for the API, for example:

```text
sync.example.com
```

The domain is for callers of the API. PostgreSQL should keep using private hostnames or IP addresses; do not expose either database through this subdomain.

### DNS

At your DNS provider, create an `A` record:

```text
Host:  sync
Type:  A
Value: the API VM public IP
TTL:   300
```

This makes `sync.example.com` resolve to the API VM. Open ports `80` and `443` on the VM firewall. Keep port `8000` bound only to `127.0.0.1`, as shown in the systemd service.

### Caddy reverse proxy

Install Caddy, then create `/etc/caddy/Caddyfile`:

```caddy
sync.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy automatically obtains and renews the TLS certificate once the DNS record points to the VM and ports `80` and `443` are reachable.

```bash
sudo systemctl reload caddy
curl --fail-with-body https://sync.example.com/docs
```

If you use Cloudflare, start with the DNS record set to **DNS only** until Caddy has obtained its certificate. Then enable proxying only if needed and use Cloudflare SSL/TLS mode **Full (strict)**.

HTTPS does not replace API authentication. Keep `X-API-Key`, firewall restrictions, and the database network rules described above.

## First sync

First check the discovered FK plan without writing rows. In production, omit database overrides because the VM `.env` holds the connection settings:

```bash
export SYNC_API_KEY='replace-with-the-api-key'

curl --fail-with-body https://sync.example.com/api/sync \
  -H "X-API-Key: $SYNC_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "tables": ["custom_form_entries"],
    "dry_run": true
  }'
```

Review the returned plan. For the current example it should include parents before the child, such as:

```text
users
custom_forms
custom_form_entries
```

Then perform one baseline copy:

```bash
curl --fail-with-body https://sync.example.com/api/sync \
  -H "X-API-Key: $SYNC_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "tables": ["custom_form_entries"],
    "full_resync": true
  }'
```

`full_resync` ignores saved cursors and reads all matching source rows. Existing target rows with the same primary key are updated; missing rows are inserted. Use it for the first load, after rebuilding the target, or to repair a missing-data incident.

## Normal incremental sync

For routine syncs, omit `full_resync`:

```bash
curl --fail-with-body https://sync.example.com/api/sync \
  -H "X-API-Key: $SYNC_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"tables": ["custom_form_entries"]}'
```

The API uses each table's `updated_at` value and scoped cursor state to fetch only changed rows. It returns `409` if another sync is already running.

For one-click operation, a normal sync also checks whether the target looks rebuilt. If saved cursors exist but a target table in the sync plan is empty, the API automatically clears the saved cursors for that source/target/table plan and treats that run as a full resync. The response includes:

```json
{
  "full_resync": true,
  "auto_full_resync": true
}
```

This lets non-technical users press the same sync button after an accidental target reset. A manual `full_resync` is still useful when the target has partial stale data rather than clearly empty tables.

### Sync response contract

`POST /api/sync` returns a consistent wrapper that is easy for another application to integrate:

```json
{
  "success": true,
  "mode": "incremental",
  "full_resync": false,
  "auto_full_resync": false,
  "message": "Sync completed successfully.",
  "data": {
    "tables": []
  },
  "errors": []
}
```

Use these fields in the caller UI:

- `success`: show success or error state after the request finishes
- `mode`: show whether the run was `dry_run`, `incremental`, or `full_resync`
- `auto_full_resync`: show a note that the API repaired an empty/reset target automatically
- `message`: display this directly to non-technical users
- `data.tables`: table-level fetched/synced/error counts
- `errors`: machine-readable list for logs, support screens, or admin details

For button UX, disable the sync button and show a spinner while the HTTP request is pending. When the response returns, hide the spinner and display `message`.

Check the recent audit trail:

```bash
curl --fail-with-body https://sync.example.com/api/sync/history \
  -H "X-API-Key: $SYNC_API_KEY"
```

## Operating rules

- Run a `dry_run` after production schema migrations, before adding a new selected table, and before a first sync to a new target.
- Run `full_resync` after recreating the target database, restoring an old backup, or repairing missing records. If users only have one sync button, the API auto-promotes normal sync to full resync when it detects empty target tables with existing cursors.
- Do not run `full_resync` for normal button clicks; it reads and upserts every matching source row and becomes slow for large tables.
- Back up `/var/lib/db_sync_api/sync_meta.db`. It holds sync cursors and history.
- Alert on failed runs, repeated `409` responses, source/target connection failures, and tables skipped because a parent failed.
- Keep source and target schema migrations coordinated. The API discovers FK order, but it cannot repair a target that is missing a required table or column.

## Known current limitations

- Deletes are not replicated. A row that stops matching `review_status = 'passed'` remains in the target until deletion/reconciliation is implemented.
- Composite primary keys are not yet supported.
- Circular and self-referencing foreign keys are rejected until a two-phase or deferred-constraint strategy is added.
- Parent tables are currently synced as complete tables. A future optimization can backfill only parent IDs referenced by selected child rows.
- The current design is single-instance. To scale horizontally, move cursor state into a shared database and replace the file lock with a distributed lock.
