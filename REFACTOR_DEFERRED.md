# Deferred work after 10-phase motion-events refactor

Shipped in Phases 1-10 (April 19, 2026): motion-events-as-primary-artifact
refactor — events.metadata_jsonb schema, metadata aggregator, polymorphic
clip URIs, server-side segment concat, AI search adapted, UI rewrites.

## Intentionally deferred

### Legacy event types still in DB + state_machine.py

`entered_scene`, `exited_scene`, `stopped`, `loitering` are still emitted
by `services/event-engine/state_machine.py` on tracklet transitions. They
are NOT surfaced in `/search` or `/timeline` (both filter to
`event_type='motion'`). They accumulate quietly in the DB.

To fully remove:
- Gut state_machine.py EventType enum down to motion-only
- Remove references from event-engine main.py (`_DEDUP_EVENT_TYPES`, cooldown)
- Remove references from nlp_search.py, zones.py, Timeline.tsx, Pictograms.tsx
- Tighten `CHECK (event_type = 'motion')` on events table
- Bulk DELETE of existing legacy rows

Estimated effort: 3-4 hours. Regression risk: moderate (state machine is
core to event-engine).

### Frame storage (frame-blobs + decoded-frames) as pipeline IPC

MinIO buckets `frame-blobs` and `decoded-frames` are used as message bus
between pipeline stages:
- edge-agent writes JPEGs → `frame-blobs`
- decode-service reads `frame-blobs`, writes decoded pixels to `decoded-frames`
- inference-worker reads `decoded-frames` for detection
- clip-service and lpr-service also read `decoded-frames`

To eliminate these buckets (and reduce disk churn), the pipeline needs
refactoring to use Kafka messages carrying binary payloads, or shared
memory, or a different IPC mechanism.

Estimated effort: 8-16 hours. Regression risk: high.

### clip_uri NOT NULL

Motion events can close without a clip when:
- Recorder is down / never started for this camera
- Segment race (Phase 9's fix handles most cases optimistically)

Rather than NOT NULL, the UI gracefully shows "No clip" for null-URI events.
If the operational fleet is reliable, this constraint could be tightened
after observing that null-URI events no longer occur in a stable week.

### HEVC segments break browser playback

Dahua cameras output HEVC (h.265) over RTSP. recorder-service stream-copies
(`-c copy`), so segments land as HEVC in MPEG-TS. `/clips/range` concats
into MP4 still as HEVC, and Firefox / Chrome refuse to play HEVC in the
`<video>` tag. Browser shows "Clip format not supported".

Also: `recorder-service/main.py:171` hardcodes `codec='h264'` in the DB
row, which is a lie — the actual codec is HEVC.

**Attempted fix (2026-04-19):** reorder `infra/dev/go2rtc.yaml` to list
the ffmpeg h.264 transcode first under each `cam-N` stream. Tried two
patterns:
- `ffmpeg:cam-N-raw#video=h264` with a named internal reference
- `ffmpeg:rtsp://<camera>/...#video=h264` pointing directly at the camera

Both produced unstable output: go2rtc's ffmpeg child process exited with
EOF within seconds, repeatedly restarting. Segments were written but
contained only 1-5 decoded frames across 30s (190-230KB vs the healthy
6MB+ HEVC baseline). Running the equivalent ffmpeg command directly
inside the go2rtc container (ffmpeg 8.0.1) produced 0 bytes — the HEVC
decoder connects, parses the SDP, creates the libx264 output, then
stalls without emitting frames. Likely cause: Dahua's HEVC sprop-vps/sps
parameter-sets aren't reliably consumed by ffmpeg 8's HEVC decoder in
real-time from this RTSP source.

Reverted the config. HEVC pipeline remains stable at ~6MB/30s, 25fps.

**Paths forward, in increasing order of effort:**
1. Configure Dahua main stream (Channels/101) to H.264 natively via the
   camera's web UI. Zero transcode overhead, permanent fix. Requires
   physical/UI access to each camera.
2. Use the camera sub-stream (Channels/102) which is already h.264 but
   at 640×360 — acceptable for low-bandwidth thumbnails, too low for
   primary playback.
3. On-demand transcode in `/clips/range`: decode HEVC + re-encode h.264
   when streaming to browser. Adds ~3-5s latency per clip, rules out
   NVR-style continuous scrubbing.
4. Swap go2rtc's ffmpeg for a hardware-decode pipeline (NVDEC / QSV /
   VA-API). Requires host GPU access and matching ffmpeg build.

Until one of these ships, the browser `<video>` tag shows
"Clip format not supported or corrupted" (handled by ClipPlayer's
onError fallback from Phase 9). The clips themselves are valid MP4 and
play correctly in VLC/mpv/ffplay.
