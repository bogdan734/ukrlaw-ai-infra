# GPU monitoring
DCGM exporter, node-exporter, Prometheus, Alertmanager (Telegram) and Grafana for one 8-GPU host.
Prereqs: driver R580 and nvidia-container-toolkit (`sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`).
Token: write it to `secrets/telegram_token`, then `sudo chown 65534 secrets/telegram_token && chmod 400 secrets/telegram_token` (Alertmanager runs as nobody); keep `secrets/` out of git.
Set both `chat_id` values in alertmanager.yml; Alertmanager will not start with the placeholder 0.
Start: `docker compose up -d` from this directory.
Test rules: `docker run --rm -v "$PWD":/w -w /w --entrypoint promtool prom/prometheus:v3.5.0 test rules alerts.test.yml`.
Check routing: `docker run --rm -v "$PWD":/w -w /w --entrypoint amtool prom/alertmanager:v0.28.1 check-config alertmanager.yml`.
Ports bind to 127.0.0.1 only; tunnel in with `ssh -L 3000:localhost:3000 -L 9090:localhost:9090 <host>`.
Grafana (change admin/admin on first login): add datasource `http://prometheus:9090` and import dashboards 12239 (DCGM) and 1860 (node).
Verify the custom counters loaded: `curl -s localhost:9400/metrics | grep -c DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` should print a nonzero count.
After editing alerts.yml run `docker compose kill -s HUP prometheus`; GpuMissing hardcodes 8 GPUs.
