---
version: "1.0.0"
status: P2-X02
created_by: codex-cli
date: "2026-04-10"
---

# Camera Onboarding Runbook

**Related documents:** `docs/deployment-guide-pilot.md`, `scripts/pilot/add-camera.sh`, `scripts/calibration/edge_filter_calibration.py`, `services/query-api/routers/topology.py`
**Scope:** Step-by-step procedure for adding a camera to the pilot or multi-node Cilex Vision deployment.

---

## Overview

Use this runbook every time a new camera is installed. The onboarding is complete only when:

1. the camera responds on RTSP,
2. the edge agent is reading it,
3. the camera exists in topology,
4. calibration has been run,
5. detections appear in the pipeline,
6. dashboards show healthy metrics.

---

## 1. Physical Setup

### Checklist

- Camera is mounted securely.
- PoE or local power is stable.
- Camera is connected to the correct camera VLAN.
- Static IP or DHCP reservation is recorded.
- The installer has recorded:
  - camera ID
  - camera name
  - site ID
  - RTSP URL
  - switch port
  - VLAN ID
  - physical location description

### Record the Camera

Use this worksheet before touching software:

| Field | Value |
|-------|-------|
| Camera ID | `cam-_____` |
| Camera name | |
| Site ID | |
| VLAN | |
| IP address | |
| RTSP URL | |
| Mount location | |

---

## 2. Verify RTSP

Run this from the host that can reach the camera:

```bash
ffprobe -v quiet -rtsp_transport tcp -i "rtsp://USER:PASS@CAMERA_IP/STREAM"
```

If the camera does not respond:

1. confirm power,
2. confirm the switch port and VLAN,
3. confirm the username and password,
4. confirm port `554` is open.

Common RTSP patterns:

- Hikvision: `rtsp://admin:pass@IP:554/Streaming/Channels/101`
- Dahua: `rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0`
- Generic: `rtsp://admin:pass@IP:554/stream1`

Do not continue until the RTSP check succeeds or site operations has approved a known temporary exception.

---

## 3. Add the Camera to the Edge Agent

### Pilot

Use the helper script:

```bash
bash scripts/pilot/add-camera.sh \
  --id cam-5 \
  --url "rtsp://USER:PASS@CAMERA_IP/STREAM" \
  --name "Rear Entrance"
```

This will:

- test RTSP,
- append the camera to `infra/pilot/cameras.yaml`,
- insert the camera into the `cameras` table,
- tell you to restart the edge agent.

Restart the edge agent:

```bash
docker restart pilot-edge-agent
```

### Multi-Node

1. Edit `infra/ansible/inventory/production.yml`.
2. Under the correct `edge_gateways` host, add the camera to its `edge_cameras` list.
3. Re-run the playbook for that site:

```bash
ansible-playbook -i infra/ansible/inventory/production.yml \
  infra/ansible/playbooks/deploy-multi-node.yml \
  --limit edge-site-a.edge.cilex.internal,monitoring-1.core.cilex.internal
```

4. Confirm the edge gateway metrics endpoint is alive:

```bash
curl -fsS http://EDGE_GATEWAY_HOST:9090/metrics
```

---

## 4. Register the Camera in Topology

### Preferred Method: Query API

If an admin cookie already exists, add the camera through the topology API:

```bash
curl -sS -X POST http://QUERY_API_HOST:8000/topology/SITE_UUID/cameras \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=PASTE_ADMIN_COOKIE" \
  -d '{
    "camera_id": "cam-5",
    "name": "Rear Entrance",
    "zone_id": "rear-entrance",
    "latitude": 40.7132,
    "longitude": -74.0048,
    "location_description": "Rear loading entrance"
  }'
```

Then create at least one edge:

```bash
curl -sS -X PUT http://QUERY_API_HOST:8000/topology/SITE_UUID/edges \
  -H "Content-Type: application/json" \
  -H "Cookie: access_token=PASTE_ADMIN_COOKIE" \
  -d '{
    "camera_a_id": "cam-corridor",
    "camera_b_id": "cam-5",
    "transition_time_s": 12,
    "confidence": 0.90,
    "enabled": true
  }'
```

### Fallback Method: Direct SQL

Use this only if the topology API is unavailable.

Add the camera:

```bash
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c "
INSERT INTO cameras (camera_id, site_id, name, status, location_description, config_json)
VALUES ('cam-5', 'SITE_UUID', 'Rear Entrance', 'offline', 'Rear loading entrance', '{\"zone_id\":\"rear-entrance\"}'::jsonb)
ON CONFLICT (camera_id) DO UPDATE
SET name = EXCLUDED.name,
    location_description = EXCLUDED.location_description,
    config_json = EXCLUDED.config_json;
"
```

Add the edge:

```bash
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics -c "
INSERT INTO topology_edges (camera_a_id, camera_b_id, transition_time_s, confidence, enabled)
VALUES ('cam-corridor', 'cam-5', 12, 0.90, true)
ON CONFLICT (camera_a_id, camera_b_id) DO UPDATE
SET transition_time_s = EXCLUDED.transition_time_s,
    confidence = EXCLUDED.confidence,
    enabled = EXCLUDED.enabled;
"
```

### Verify Topology Registration

```bash
curl -fsS http://localhost:8080/topology/SITE_UUID | python3 -m json.tool | head -80
```

Check that:

- the new camera appears,
- at least one edge connects it to the site graph,
- `zone_id` is present if events or loitering depend on it.

---

## 5. Run Edge Calibration

Run the calibration harness after the camera is live:

```bash
python3 scripts/calibration/edge_filter_calibration.py \
  --camera-id cam-5 \
  --edge-config infra/pilot/cameras.yaml \
  --window-s 600
```

If the output recommends new thresholds, record them in the camera configuration or change-management record before the next rollout.

If the camera is in a low-motion area, run calibration during representative operating hours.

---

## 6. Monitoring Verification

### Prometheus

Pilot:

```bash
curl -fsS http://localhost:9090/api/v1/targets | python3 -m json.tool | head -80
```

Multi-node:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

### Grafana

Open:

- Stream Health: `/d/stream-health`
- Inference Performance: `/d/inference-perf`
- Bus Health: `/d/bus-health`

Confirm the new camera appears in Stream Health and shows:

- uptime greater than `0`,
- non-zero motion or static counters,
- no sustained decode errors.

---

## 7. Validate End-to-End Pipeline Flow

### Quick Checks

1. Edge agent logs:

```bash
docker logs pilot-edge-agent --tail 50 | grep -Ei "cam-5|connected|publish"
```

2. Query API health:

```bash
curl -fsS http://localhost:8080/health
```

3. Detections arriving:

```bash
curl -sS "http://localhost:8080/detections?camera_id=cam-5&limit=5" \
  -H "Cookie: access_token=PASTE_OPERATOR_COOKIE" | python3 -m json.tool
```

4. Tracks arriving:

```bash
curl -sS "http://localhost:8080/tracks?camera_id=cam-5&limit=5" \
  -H "Cookie: access_token=PASTE_OPERATOR_COOKIE" | python3 -m json.tool
```

5. Optional Kafka confirmation:

```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 \
  --topic tracklets.local \
  --max-messages 5
```

### Success Criteria

The camera is considered onboarded only if all are true:

- RTSP is reachable
- edge agent is connected
- topology entry exists
- calibration completed
- detections or tracklets are visible
- Grafana shows healthy metrics

---

## 8. Rollback

If the camera causes instability:

### Pilot

1. Disable or remove the camera from `infra/pilot/cameras.yaml`.
2. Restart the edge agent:

```bash
docker restart pilot-edge-agent
```

3. Remove the topology entry:

```bash
curl -sS -X DELETE http://localhost:8080/topology/SITE_UUID/cameras/cam-5 \
  -H "Cookie: access_token=PASTE_ADMIN_COOKIE"
```

### Multi-Node

1. Remove the camera from `infra/ansible/inventory/production.yml`.
2. Re-run the same `deploy-multi-node.yml --limit ...` command used during onboarding.
3. Remove the topology entry through the API or SQL.

### After Rollback

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

Log the rollback reason and keep the original RTSP details for follow-up.
