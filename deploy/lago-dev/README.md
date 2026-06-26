# lago-dev — self-hosted Lago billing engine (VR3 dev)

Dev instance of [Lago](https://github.com/getlago/lago) (v1.48.1) backing the VR3AI billing platform.
Runs as a Dockge stack on the TrueNAS box **192.11.0.10**, Postgres+pg_partman (no ClickHouse — opt-in only).

## Topology

| Service | Network | Address |
| --- | --- | --- |
| `api` | internal bridge + **macvlan 192.11.0.39** | `http://192.11.0.39:3000` — Dograh → Lago server-to-server |
| `front` | internal bridge + **macvlan 192.11.0.40** | `http://192.11.0.40` — admin UI |
| `db`, `redis`, `api-worker`, `api-clock`, `pdf` | internal bridge only | not exposed |

macvlan network is the shared `macvlan-network` (external, subnet 192.11.0.0/24, gw .254, parent enp129s0).
**Note:** the Docker host itself cannot reach the macvlan IPs (host isolation) — test from another LAN host or a container, not from the TrueNAS shell.

## Deploy

Stack lives at `/mnt/.ix-apps/app_mounts/dockge/stacks/lago-dev/` on 192.11.0.10 (`compose.yaml` + `.env`).

```bash
# on the box (or via Dockge):
cd /mnt/.ix-apps/app_mounts/dockge/stacks/lago-dev
docker compose up -d            # .env auto-loaded; pulls getlago/* images
docker compose ps              # all 7 services Up; db/redis/api/api-worker healthy
```

The real `.env` (secrets) lives only on the box — see `.env.example` for the keys + how to generate them.
On first boot, `LAGO_CREATE_ORG=true` seeds the **VR3AI** org + an admin user + the API key (`LAGO_ORG_API_KEY`).

## Billable metrics + plan (seeded via API)

Two `sum_agg` metrics on `field_name=value`, `recurring=false`:

- `voice_minutes`
- `ai_cost_cents`

Plan `growth-test` (monthly): two `package` charges — voice_minutes (free_units 1000, $0.04/unit), ai_cost_cents (free_units 50000, $0.012/unit). Numbers are placeholders; real tiers are business-owned, set in the admin UI.

Create/verify via the API (Bearer = `LAGO_ORG_API_KEY`):

```bash
curl -s -H "Authorization: Bearer $LAGO_ORG_API_KEY" http://192.11.0.39:3000/api/v1/billable_metrics
curl -s -H "Authorization: Bearer $LAGO_ORG_API_KEY" http://192.11.0.39:3000/api/v1/plans
```

## Dograh integration

The Dograh stack's env points at this instance:

```
LAGO_API_URL=http://192.11.0.39:3000
LAGO_API_KEY=<LAGO_ORG_API_KEY>
LAGO_WEBHOOK_SECRET=<shared secret; set a Lago webhook -> https://<dograh>/api/v1/billing/webhooks/lago>
```

## TODO

- Public host `vr3ai-billing-dev.tapcloud.org` + nginx + TLS (currently admin UI is LAN-only at http://192.11.0.40).
- Pin/track Lago version on upgrades (currently v1.48.1).
