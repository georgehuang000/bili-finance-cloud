# Ubuntu VPS deployment

The API keeps CLOUD_MODE=true and its database role separate from the worker.
Caddy serves the built web/dist and terminates HTTPS; Uvicorn only listens on
127.0.0.1:9000 and trusts forwarding metadata from the loopback proxy.

- Code/releases: /opt/bili-finance/releases/<commit>; current is a symlink.
- Isolated Python runtime: /opt/bili-finance/venv.
- Root-only environment files: /etc/bili-finance/api.env and worker.env (0600).
- OS users: bili-api and bili-worker, read-only code group bili-app.
- Install these unit files under /etc/systemd/system and the Caddyfile under
  /etc/caddy. Validate with systemd-analyze verify and caddy validate first.
- Only activate worker/report timers after stopping the GitHub worker and
  confirming no queued/running job is executing on the old runner.
- Daily report timers use Asia/Shanghai at 08:00 and 18:00; Persistent=true
  catches a missed slot after reboot. The API never starts an in-process
  scheduler. A single flock serializes all VPS workers. Enqueue runs before
  that lock, so a busy queue worker does not discard a scheduled report.
- Neon remains the database during the first migration stage. Use its existing
  restricted API and worker URLs and encryption key, not the database owner URL.
- The public-IP certificate uses Let's Encrypt's shortlived profile and Caddy
  renewal. Replace the site address/default_sni for a user-owned domain later.
- The same protected site is also available over HTTPS on port 10000. Allow
  TCP 10000 in the cloud firewall, and retain 80/443 for certificate validation.
  Port 9000 remains loopback-only; frontend requests use same-origin /api.

Operational checks: systemctl status bili-finance-api; systemctl list-timers
'bili-finance-*'; journalctl -u bili-finance-worker --since today. Never print
environment files, model keys, Bili cookies, MFA seeds or recovery codes.
