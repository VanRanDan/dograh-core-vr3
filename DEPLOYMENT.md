# VR3 AI Docs Deployment

**Live at:** https://docs.vr3ai.tapcloud.org

## Infrastructure

- **Host:** TrueNAS (192.11.0.10)
- **Stack:** `/mnt/.ix-apps/app_mounts/dockge/stacks/vr3-docs/`
- **Service IP:** 192.11.0.37 (macvlan)
- **Frontend:** Traefik (192.11.0.187) → HTTP:80 on 192.11.0.37

## Containers

- **vr3-docs-app** — Node.js/Mintlify dev server on port 3000 (internal)
- **vr3-docs-nginx** — nginx:alpine reverse proxy on 192.11.0.37:80

## UI Customization

### Colors (docs.json)
- **Primary:** `#2BB5D8` (cyan — from VR3 logo text)
- **Light:** `#5CCCEC` (lighter cyan)
- **Dark:** `#1B3A8A` (navy — from VR3 logo figure)

### Branding
- **Logo:** `vr3logo.png` (light and dark)
- **Logo height:** 56px
- **Favicon:** VR3 logo
- **Site name:** "VR3 AI"
- **Search:** Disabled (Mintlify cloud search unavailable self-hosted)

## Update Workflow

### Edit docs locally
```bash
cd C:\Users\Danny\repos\dograh-core-vr3
# Edit files in docs/**/*.mdx or docs/docs.json
git add docs/
git commit -m "docs: <description>"
git push origin main
```

### Deploy to TrueNAS
```bash
# SSH to 192.11.0.10, then:
sudo docker compose -f /mnt/.ix-apps/app_mounts/dockge/stacks/vr3-docs/compose.yaml build --no-cache docs
sudo docker compose -f /mnt/.ix-apps/app_mounts/dockge/stacks/vr3-docs/compose.yaml up -d docs
```

Or use Python/paramiko for automated deployment.

### Sync upstream Dograh docs
```bash
git fetch upstream
git merge upstream/main
# Resolve any conflicts (likely in docs.json)
git push origin main
# Redeploy
```

## Key Files

- **GitHub repo:** https://github.com/VanRanDan/dograh-core-vr3
- **Stack config:** `/mnt/.ix-apps/app_mounts/dockge/stacks/vr3-docs/compose.yaml`
- **Nginx config:** `/mnt/.ix-apps/app_mounts/dockge/stacks/vr3-docs/nginx/vr3-docs.conf`
- **Dockerfile:** `/mnt/.ix-apps/app_mounts/dockge/stacks/vr3-docs/Dockerfile`

## Recent Changes

| Commit | Description |
|--------|-------------|
| `300a5ce` | config: set site URL to docs.vr3ai.tapcloud.org |
| `adbd10e` | ui: VR3 logo as favicon, increase logo height to 56px |
| `2900bd5` | fix: hide search bar via CSS (mintlify cloud search unavailable self-hosted) |
| `5021acc` | config: disable mintlify cloud search |
| `0e10411` | rebrand: update theme colors to VR3 AI blue palette |

## Troubleshooting

**Container not responding:**
- Check TrueNAS: `docker ps | grep vr3-docs`
- Check logs: `docker logs vr3-docs-app` or `docker logs vr3-docs-nginx`
- Verify nginx config: `docker exec vr3-docs-nginx nginx -t`
- Test internally: `docker exec vr3-docs-nginx curl http://vr3-docs-app:3000/`

**Network unreachable:**
- Verify macvlan IP: `docker inspect vr3-docs-nginx | grep "192.11.0.37"`
- Confirm Traefik can reach 192.11.0.37 from its network
- Check DNS: `nslookup docs.vr3ai.tapcloud.org` (should resolve to Traefik frontend IP)
