# Single-VM paper deployment

Use one Linux VM with 1 vCPU, 1–2 GB RAM, a persistent disk, Docker Engine, and
the Compose plugin. Allow inbound SSH only; block all other inbound ports.
Keep this repository and `/etc/memecoin-bot/bot.env` readable only by VM admins.
For Ubuntu, follow [Docker's Engine installation guide](https://docs.docker.com/engine/install/ubuntu/),
which includes the Compose plugin.

1. Clone the repository, then create the external environment file:
   `sudo install -d -m 700 /etc/memecoin-bot && sudoedit /etc/memecoin-bot/bot.env`.
   Set its mode to 600 with `sudo chmod 600 /etc/memecoin-bot/bot.env`.
2. Set `SOLANA_RPC_URL`, `JUPITER_API_KEY`, `TELEGRAM_BOT_TOKEN`, and numeric
   `TELEGRAM_CHAT_ID` there. Also set `LIVE_TRADING_ENABLED=false`,
   `NARRATIVE_OPENAI_ENABLED=false`, `ALERTS_ENABLED=true`,
   `SOLANA_KEYPAIR_PATH=/app/state/paper-keypair.json`,
   `TRADE_STATE_PATH=/app/state/trade-state.sqlite3`, and
   `OPERATIONS_BACKUP_DIRECTORY=/app/state/backups`. Carry over any frozen
   strategy settings from the existing paper run. Never copy or fund the burner
   wallet on this VM. The Compose file forces live mode off and paper autostart on.
3. Run `sudo docker compose build`, then
   `sudo docker compose run --rm --no-deps worker init-paper-identity`. This creates
   one unfunded keypair with owner-only permissions in the named state volume;
   it never prints the secret and refuses to overwrite an existing identity.
4. Run `sudo docker compose up -d`. Check `sudo docker compose ps`,
   `sudo docker compose exec worker memecoin-bot paper-health`, and
   `sudo docker compose logs --tail=100 worker alerts`. The worker may be in its
   five-minute healthcheck start period while the first cycles finish.

The worker uses host networking solely so its existing `127.0.0.1:8787`
dashboard can be reached through SSH. Compose publishes **no ports**. On your
computer, run `ssh -N -L 8787:127.0.0.1:8787 USER@VM`, then open
`http://127.0.0.1:8787`. Read the one-time pairing code from the worker logs;
do not share those logs. The separate alerts container remains running if the
worker stops. It sends a startup notice, daily heartbeat, and deduplicated
issue/recovery messages. Docker marks a stale worker unhealthy; it does not
automatically restart a still-running but unhealthy process.

To prove monitoring, run `sudo docker compose stop worker` **without** stopping
`alerts`. After the collector/exit freshness threshold plus one 60-second alert
poll, confirm one Telegram alert. Run `sudo docker compose start worker`, wait for a
healthy cycle, and confirm recovery. A failed alert test blocks reliance on the
deployment. `restart: unless-stopped` restarts crashes, while persisted alert
state prevents repeated issue and startup messages during rapid restarts.

For updates: `git pull --ff-only`, `sudo docker compose build`, then
`sudo docker compose up -d --force-recreate`. Watch the first cycles and repeat the
health and alert checks. To stop intentionally, run `sudo docker compose stop alerts`
then `sudo docker compose stop worker`; keep the named volume. Do not use
`docker compose down -v` because it deletes the journal, auth file, identity,
and backups. Only one worker container may use `memeops-paper-state`; its
`fcntl` lock prevents a second owner of that volume, not a separate volume.

SQLite backups go to `/app/state/backups` on the same persistent volume. Copy
them off the VM regularly: create a private host directory with
`sudo install -d -m 700 /var/backups/memeops`, then use
`sudo docker cp memeops-paper-worker:/app/state/backups/. /var/backups/memeops/`.
Run `sudo chown -R "$USER":"$USER" /var/backups/memeops` before transfer.
From a trusted computer, use `scp -r USER@VM:/var/backups/memeops LOCAL_DIR`;
restrict permissions at the destination. Keep an off-host copy because a VM or
disk loss also destroys the named volume.

For a clean forward campaign, let the initial journal collect research first.
Stop `alerts`, then `worker`; run
`sudo docker compose run --rm --no-deps worker new-paper-run --output /app/state/forward-paper.sqlite3`.
Change only `TRADE_STATE_PATH` in the external env file to that new path and run
`sudo docker compose up -d --force-recreate`. The old journal stays in the volume;
the new one copies research but starts with no trades. The new journal has a
separate dashboard auth file and requires one new pairing. Do not carry a
partly monitored old campaign across hosts: archive its backup off-host and
start fresh so any transfer gap cannot count as observed exits.
