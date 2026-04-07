---
version: "1.0.0"
status: P1-X01
created_by: scaffold
authored_by: codex-doc-agent
date: "2026-04-07"
---

# Camera Compatibility Matrix

This matrix tracks which IP cameras are known to work with the Cilex Vision ingest path:

- ONVIF for discovery and capability checks
- RTSP over TCP into the edge agent's GStreamer `rtspsrc ! decodebin` pipeline
- H.264/H.265 streams suitable for central decode and analytics

No physical cameras are available in this environment, so the first two entries below are seeded from published vendor specifications and marked `UNTESTED`. The companion tooling in `scripts/camera-compat/` is the real probe path for moving rows from `UNTESTED` to `TESTED-OK` or `TESTED-ISSUES`.

## Status meanings

- `TESTED-OK`: live ONVIF and RTSP probe succeeded and no blocking issue was found.
- `TESTED-ISSUES`: the probe reached the camera but found a compatibility gap or partial failure.
- `UNTESTED`: matrix row is based on published specifications only; no live probe has been run.

## Current matrix

| Brand | Model | Firmware | ONVIF | RTSP | H.265 | Dual Stream | Triple Stream | IR Detect | IK10 | Smart Codec | Status |
|-------|-------|----------|-------|------|-------|-------------|---------------|-----------|------|-------------|--------|
| Dahua | IPC-HDW5449T (WizMind) | Unverified; closest reviewed family datasheet is Rev 002.000 dated 2023-12-12 | Profile S/G/T | Yes | Yes | Yes | Yes | No IR; full-color warm LED family | Vendor docs reviewed here do not confirm exact chassis rating | Smart H.264+/H.265+, AI H.264/H.265 | UNTESTED |
| Hikvision | DS-2CD2387G2-LU (ColorVu) | `Firmware_V5.7.23_260114` offered on vendor page; unverified on device | Profile S/G/T | Yes | Yes | Yes | Yes | No IR; white-light ColorVu family | Not listed on the reviewed vendor product page | H.265+ | UNTESTED |

## Published sources and caveats

### Dahua IPC-HDW5449T (WizMind)

- Reviewed source: Dahua official `DH-IPC-HDW5449TM-SE-LED` WizMind datasheet, which is the closest public 5449T-family turret/eyeball spec I could verify in this environment.
  - https://material.dahuasecurity.com/uploads/cpq/prm-os-srv-res/smart/datasheetzipfiles/IPC-HDW5449TM-SE-LED_S2_datasheet_20231212.pdf
- Published data used for the row:
  - ONVIF `Profile S`, `Profile G`, `Profile T`
  - `RTP`, `RTSP`, `RTCP`
  - `H.265`, `H.264`, `MJPEG`
  - `3 streams`
  - `Smart H.265+`, `Smart H.264+`, `AI H.265`, `AI H.264`
  - full-color warm-light operation instead of an IR cutover family
- Caveat:
  - the exact `IPC-HDW5449T` suffix in procurement may differ by lens, LED, or regional SKU. Probe the exact shipped unit before changing this row to `TESTED-*`.

### Hikvision DS-2CD2387G2-LU (ColorVu)

- Reviewed source: Hikvision official product page for `DS-2CD2387G2-LU`.
  - https://www.hikvision.com/us-en/products/network-products/network-cameras/colorvu-series/ds-2cd2387g2-lu/
- Published data used for the row:
  - ONVIF `Profile S`, `Profile G`, `Profile T`
  - `RTP`, `RTSP`
  - `H.265/H.264/H.264+/H.265+`
  - main stream + sub-stream + third stream
  - 24/7 color / white-light design rather than IR mode switching
  - current public firmware listing `Firmware_V5.7.23_260114`
- Caveat:
  - the reviewed vendor page clearly lists `IP67`, but not `IK10`. Treat vandal resistance as unverified until the exact regional datasheet or shipped unit is checked.

## Selection guidance for this platform

The platform detects `person`, `car`, `truck`, `bus`, `bicycle`, `motorcycle`, and `animal`. For that workload, camera selection should optimize for stream stability first and analytics quality second.

- Prefer cameras with ONVIF `Profile T` or at minimum `Profile S`, plus RTSP over TCP. This matches the edge agent's `rtspsrc location="..." latency=100 protocols=tcp ! decodebin` connection pattern.
- Prefer cameras that expose at least three streams. The platform can use a high-quality main stream while still keeping a lower-resolution substream available for debugging or future edge-side profiles.
- Prefer published `H.265` support with a fallback `H.264` path. This gives better storage efficiency without locking the deployment to one codec family.
- Prefer large sensors, strong low-light performance, and published WDR. Small classes such as `bicycle`, `motorcycle`, and `animal` degrade first at night or under backlight.
- If the site needs infrared behavior, do not assume a ColorVu/full-color family will behave like an IR-cut family. These two starter rows are visible-light-first designs.
- If the site needs vandal resistance, verify the exact SKU's `IK10` claim from the shipping datasheet, not just the product family page.

## Probe workflow

### 1. Probe a single camera

Command:

```bash
python3 scripts/camera-compat/probe_camera.py \
  --brand Hikvision \
  --model DS-2CD2387G2-LU \
  --host 192.0.2.10 \
  --username admin \
  --password 'REPLACE_ME'
```

Expected output:

- JSON report to stdout
- `status` is `TESTED-OK` when RTSP frame read succeeds and no blocking ONVIF error occurs
- `matrix_columns` contains every column needed for the Markdown matrix

### 2. Seed a row from published data only

Command:

```bash
python3 scripts/camera-compat/probe_camera.py \
  --brand Dahua \
  --model 'IPC-HDW5449T (WizMind)' \
  --published-only \
  --firmware 'Unverified; closest reviewed family datasheet is Rev 002.000 dated 2023-12-12' \
  --onvif-summary 'Profile S/G/T' \
  --rtsp-summary 'Yes' \
  --h265 yes \
  --dual-stream yes \
  --triple-stream yes \
  --ir-detect 'No IR; full-color warm LED family' \
  --ik10 'Vendor docs reviewed here do not confirm exact chassis rating' \
  --smart-codec 'Smart H.264+/H.265+, AI H.264/H.265'
```

Expected output:

- JSON report with `status: UNTESTED`
- no ONVIF or OpenCV dependency required in this mode

### 3. Run the suite against a CSV or YAML list

Command:

```bash
scripts/camera-compat/run_compat_suite.sh cameras.yaml
```

Expected output:

- one JSON report per camera under `artifacts/camera-compat/latest/reports/`
- generated Markdown matrix at `artifacts/camera-compat/latest/matrix.md`

### Example YAML inventory

```yaml
cameras:
  - brand: Dahua
    model: IPC-HDW5449T (WizMind)
    published_only: true
    firmware: Unverified; closest reviewed family datasheet is Rev 002.000 dated 2023-12-12
    onvif_summary: Profile S/G/T
    rtsp_summary: "Yes"
    h265: yes
    dual_stream: yes
    triple_stream: yes
    ir_detect: No IR; full-color warm LED family
    ik10: Vendor docs reviewed here do not confirm exact chassis rating
    smart_codec: Smart H.264+/H.265+, AI H.264/H.265
    status: UNTESTED
    sources:
      - https://material.dahuasecurity.com/uploads/cpq/prm-os-srv-res/smart/datasheetzipfiles/IPC-HDW5449TM-SE-LED_S2_datasheet_20231212.pdf
  - brand: Hikvision
    model: DS-2CD2387G2-LU (ColorVu)
    published_only: true
    firmware: Firmware_V5.7.23_260114 offered on vendor page; unverified on device
    onvif_summary: Profile S/G/T
    rtsp_summary: "Yes"
    h265: yes
    dual_stream: yes
    triple_stream: yes
    ir_detect: No IR; white-light ColorVu family
    ik10: Not listed on the reviewed vendor product page
    smart_codec: H.265+
    status: UNTESTED
    sources:
      - https://www.hikvision.com/us-en/products/network-products/network-cameras/colorvu-series/ds-2cd2387g2-lu/
```
