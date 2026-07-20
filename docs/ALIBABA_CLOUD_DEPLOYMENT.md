# Alibaba Cloud deployment

Qwendom ships as one production container: FastAPI serves the API, the built
React application, and hash-verified task artifacts from the same origin. The
container is suitable for Alibaba Cloud ECS, SAE, or ACK. For the hackathon,
ECS plus Alibaba Cloud Container Registry (ACR) is the smallest operational
surface.

## Required runtime configuration

Create secrets in the Alibaba deployment environment; never bake them into the
image:

- `LLM_PROVIDER=qwen`
- `QWEN_API_KEY`
- `QWEN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
- `QWEN_MODEL=qwen3.7-plus`
- `AGENTBAY_API_KEY`
- `AGENTBAY_ENDPOINT=wuyingai.ap-southeast-1.aliyuncs.com`
- `AGENTBAY_REGION_ID=ap-southeast-1`
- `TEAM_COMPOSITION_EXECUTION_ENABLED=true`
- `ALLOW_DETERMINISTIC_NO_KEY=false`
- `FRONTEND_ORIGIN=https://YOUR_PUBLIC_HOST`

Attach persistent storage at `/app/backend/society/data`. It contains the
event ledger and exported composition artifacts. The application never serves
an artifact by arbitrary filesystem path: downloads are task-scoped and the
recorded SHA-256 is verified before transfer.

## Build and smoke locally

```bash
docker build -t qwendom:judge .
docker run --rm --env-file backend/.env -e FRONTEND_ORIGIN=http://localhost:8000 \
  -e TEAM_COMPOSITION_EXECUTION_ENABLED=true -p 8000:8000 qwendom:judge
curl --fail http://localhost:8000/health
```

Open `http://localhost:8000`. Do not expose port 5173 in production.

## Publish to ACR

```bash
docker login --username YOUR_ACR_USER registry-intl.ap-southeast-1.aliyuncs.com
docker tag qwendom:judge registry-intl.ap-southeast-1.aliyuncs.com/YOUR_NAMESPACE/qwendom:judge
docker push registry-intl.ap-southeast-1.aliyuncs.com/YOUR_NAMESPACE/qwendom:judge
```

Deploy that immutable tag to ECS, SAE, or ACK with one replica initially. The
service must allow outbound HTTPS to DashScope/Model Studio and AgentBay;
Context7 also requires the configured MCP command when research is enabled.
Expose container port `8000` through an HTTPS listener or reverse proxy and
configure its health probe as `GET /health`.
