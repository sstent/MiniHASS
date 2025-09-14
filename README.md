# MiniHass - Smart Home Controller

## Configuration Storage in Consul

The application now uses Consul for centralized configuration management. All settings and TV credentials are stored in Consul's key-value store.

### Key Path Structure
- App configuration: `MiniHass/config`
- TV credentials: `MiniHass/tv_credentials/<tv_ip>`

### Initial Setup
1. Set environment variables in docker-compose.yml:
```yaml
environment:
  - CONSUL_HOST=consul.service.dc1.consul
  - CONSUL_PORT=8500
  - TPLINK_IP=192.168.1.100
  - TV_IP=192.168.1.101
  - TV_MAC=AA:BB:CC:DD:EE:FF
```

2. On first run, the app will:
   - Create initial configuration in Consul using environment variables
   - Store TV pairing keys in Consul when devices are paired

### Managing Configuration
- Update configuration via API:
  ```bash
  POST /api/config
  {
    "tplink_ip": "new_ip",
    "tv_ip": "new_tv_ip"
  }
  ```
- Or directly through Consul UI: http://consul.service.dc1.consul:8500/ui/dc1/kv/MiniHass/

### Health Monitoring
The health endpoint now includes Consul connectivity status:
```bash
GET /health

{
  "status": "healthy",
  "config": {...},
  "services": {
    "consul_connected": true
  }
}
```

### Docker Deployment
- Removed local volume for config storage
- Requires network access to Consul cluster

### Continuous Integration
We use GitHub Actions to automatically build and push Docker images to GitHub Container Registry.

#### Workflow Details
- Triggers on pushes to main branch
- Builds Docker image using the Dockerfile
- Pushes image to GHCR with two tags: `latest` and commit SHA

#### Using the Image
```yaml
# Example docker-compose snippet
services:
  smart-home:
    image: ghcr.io/your-username/your-repo:latest
    # ... rest of config
```

#### Status Badge
[![Build and Push Docker Image](https://github.com/OWNER/REPO/actions/workflows/container-build.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/container-build.yml)

> Replace OWNER/REPO with your GitHub username and repository name