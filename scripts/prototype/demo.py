"""Throwaway 1-camera prototype — NOT FOR PRODUCTION USE.

Single-file demo: RTSP/webcam → YOLOv8n → SQLite → Flask web UI.
Displays live MJPEG stream with bbox overlay, last-50 detection table,
and detection-count-per-minute chart for the last 30 minutes.

Environment variables:
    CAMERA_URL  — RTSP URL or webcam index (default: "0" = first webcam)
    DB_PATH     — SQLite file path (default: "detections.db")
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template_string
from ultralytics import YOLO

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CAMERA_URL: str = os.environ.get("CAMERA_URL", "0")
DB_PATH: str = os.environ.get("DB_PATH", "detections.db")
TARGET_FPS: int = 5
CONF_THRESHOLD: float = 0.40
MODEL_NAME: str = "yolov8n.pt"

# COCO class IDs → taxonomy classes (docs/taxonomy.md)
COCO_TO_TAXONOMY: dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    # COCO animals → single "animal" class
    14: "animal",  # bird
    15: "animal",  # cat
    16: "animal",  # dog
    17: "animal",  # horse
    18: "animal",  # sheep
    19: "animal",  # cow
    20: "animal",  # elephant
    21: "animal",  # bear
    22: "animal",  # zebra
    23: "animal",  # giraffe
}

# BGR colors for bounding boxes
CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "person": (0, 255, 0),
    "car": (255, 0, 0),
    "truck": (0, 165, 255),
    "bus": (0, 255, 255),
    "bicycle": (255, 255, 0),
    "motorcycle": (255, 0, 255),
    "animal": (0, 128, 255),
}


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def init_db(path: str) -> sqlite3.Connection:
    """Create the detections table if it doesn't exist."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS detections ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  timestamp TEXT NOT NULL,"
        "  class_name TEXT NOT NULL,"
        "  confidence REAL NOT NULL,"
        "  x1 INTEGER, y1 INTEGER, x2 INTEGER, y2 INTEGER"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_det_ts ON detections(timestamp)"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Detection pipeline (background thread)
# ---------------------------------------------------------------------------


class DetectionPipeline:
    """Reads frames, runs YOLOv8n, stores detections, serves annotated JPEG."""

    def __init__(self) -> None:
        self.model = YOLO(MODEL_NAME)

        cam_url: str | int = CAMERA_URL
        if isinstance(cam_url, str) and cam_url.isdigit():
            cam_url = int(cam_url)
        self.cap = cv2.VideoCapture(cam_url)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera: {CAMERA_URL}")

        self.db = init_db(DB_PATH)
        self._db_lock = threading.Lock()

        self._latest_jpeg: bytes | None = None
        self._frame_lock = threading.Lock()

        self._running = False
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self.cap.release()

    # -- main loop --

    def _run(self) -> None:
        interval = 1.0 / TARGET_FPS
        last_t = 0.0

        while self._running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(1)
                self._reconnect()
                continue

            now = time.monotonic()
            if now - last_t < interval:
                continue
            last_t = now

            results = self.model(frame, verbose=False, conf=CONF_THRESHOLD)
            annotated = frame.copy()
            ts = datetime.now(timezone.utc).isoformat()
            rows: list[tuple[str, str, float, int, int, int, int]] = []

            for r in results:
                for box in r.boxes:
                    cls_id = int(box.cls[0])
                    if cls_id not in COCO_TO_TAXONOMY:
                        continue
                    class_name = COCO_TO_TAXONOMY[cls_id]
                    conf = float(box.conf[0])
                    x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                    rows.append((ts, class_name, conf, x1, y1, x2, y2))
                    self._draw_box(annotated, class_name, conf, x1, y1, x2, y2)

            if rows:
                with self._db_lock:
                    self.db.executemany(
                        "INSERT INTO detections"
                        " (timestamp,class_name,confidence,x1,y1,x2,y2)"
                        " VALUES (?,?,?,?,?,?,?)",
                        rows,
                    )
                    self.db.commit()

            ok, jpeg = cv2.imencode(
                ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70]
            )
            if ok:
                with self._frame_lock:
                    self._latest_jpeg = jpeg.tobytes()

    def _reconnect(self) -> None:
        cam_url: str | int = CAMERA_URL
        if isinstance(cam_url, str) and cam_url.isdigit():
            cam_url = int(cam_url)
        self.cap.release()
        self.cap = cv2.VideoCapture(cam_url)

    @staticmethod
    def _draw_box(
        img: np.ndarray,
        cls: str,
        conf: float,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> None:
        color = CLASS_COLORS.get(cls, (128, 128, 128))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        label = f"{cls} {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
        )
        cv2.rectangle(img, (x1, y1 - th - 6), (x1 + tw, y1), color, -1)
        cv2.putText(
            img, label, (x1, y1 - 4),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
        )

    # -- data accessors --

    def get_frame(self) -> bytes | None:
        with self._frame_lock:
            return self._latest_jpeg

    def get_recent_detections(self, limit: int = 50) -> list[dict]:
        with self._db_lock:
            cur = self.db.execute(
                "SELECT id,timestamp,class_name,confidence,x1,y1,x2,y2"
                " FROM detections ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [
                {
                    "id": r[0],
                    "timestamp": r[1],
                    "class_name": r[2],
                    "confidence": round(r[3], 3),
                    "bbox": [r[4], r[5], r[6], r[7]],
                }
                for r in cur.fetchall()
            ]

    def get_per_minute_counts(self, minutes: int = 30) -> list[dict]:
        with self._db_lock:
            cur = self.db.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:00Z', timestamp) AS minute,"
                "       COUNT(*) AS count"
                " FROM detections"
                " WHERE timestamp >= datetime('now', ?)"
                " GROUP BY minute ORDER BY minute",
                (f"-{minutes} minutes",),
            )
            return [{"minute": r[0], "count": r[1]} for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
pipeline: DetectionPipeline | None = None

HTML_PAGE = """\
<!DOCTYPE html>
<html>
<head>
  <title>Cilex Vision — Prototype</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:monospace;background:#111;color:#eee;padding:16px}
    h1{color:#4af;margin-bottom:8px}
    .warn{background:#a00;color:#fff;padding:8px 12px;border-radius:4px;
          margin-bottom:16px;font-weight:bold}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .panel{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:16px}
    .panel h2{color:#4af;margin-bottom:12px;font-size:14px}
    img{width:100%;border-radius:4px}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{padding:4px 8px;text-align:left;border-bottom:1px solid #333}
    th{color:#888}
    .chart-box{height:250px}
    canvas{width:100%!important;height:100%!important}
    .b{display:inline-block;padding:2px 6px;border-radius:3px;font-size:11px;color:#fff}
    .b-person{background:#0c0}.b-car{background:#c00}.b-truck{background:#c80}
    .b-bus{background:#0cc}.b-bicycle{background:#cc0}.b-motorcycle{background:#c0c}
    .b-animal{background:#c60}
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
</head>
<body>
  <h1>Cilex Vision Prototype</h1>
  <div class="warn">THROWAWAY PROTOTYPE — NOT FOR PRODUCTION USE</div>
  <div class="grid">
    <div class="panel">
      <h2>LIVE FEED</h2>
      <img src="/stream" alt="Live feed"/>
    </div>
    <div class="panel">
      <h2>DETECTIONS / MINUTE (last 30 min)</h2>
      <div class="chart-box"><canvas id="chart"></canvas></div>
    </div>
    <div class="panel" style="grid-column:span 2">
      <h2>LAST 50 DETECTIONS</h2>
      <div style="max-height:400px;overflow-y:auto">
        <table id="tbl">
          <thead><tr><th>Time</th><th>Class</th><th>Conf</th><th>BBox</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>
  <script>
    const ctx=document.getElementById('chart').getContext('2d');
    const chart=new Chart(ctx,{type:'bar',
      data:{labels:[],datasets:[{label:'Detections',data:[],backgroundColor:'#4af'}]},
      options:{responsive:true,maintainAspectRatio:false,
        scales:{x:{ticks:{color:'#888',maxRotation:45},grid:{color:'#333'}},
                y:{beginAtZero:true,ticks:{color:'#888'},grid:{color:'#333'}}},
        plugins:{legend:{display:false}}}});
    async function refresh(){
      try{
        const[dr,cr]=await Promise.all([fetch('/api/detections'),fetch('/api/chart')]);
        const dets=await dr.json(), counts=await cr.json();
        document.querySelector('#tbl tbody').innerHTML=dets.map(d=>
          `<tr><td>${d.timestamp.substring(11,19)}</td>`+
          `<td><span class="b b-${d.class_name}">${d.class_name}</span></td>`+
          `<td>${d.confidence}</td><td>[${d.bbox}]</td></tr>`).join('');
        chart.data.labels=counts.map(c=>c.minute.substring(11,16));
        chart.data.datasets[0].data=counts.map(c=>c.count);
        chart.update();
      }catch(e){console.error(e)}
    }
    refresh(); setInterval(refresh,3000);
  </script>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return render_template_string(HTML_PAGE)


@app.route("/stream")
def stream() -> Response:
    """MJPEG stream of annotated frames."""
    def generate():  # type: ignore[no-untyped-def]
        while True:
            frame = pipeline.get_frame() if pipeline else None
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            else:
                time.sleep(0.1)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/detections")
def api_detections() -> Response:
    if not pipeline:
        return jsonify([])
    return jsonify(pipeline.get_recent_detections())


@app.route("/api/chart")
def api_chart() -> Response:
    if not pipeline:
        return jsonify([])
    return jsonify(pipeline.get_per_minute_counts())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pipeline = DetectionPipeline()
    pipeline.start()
    try:
        app.run(host="0.0.0.0", port=5000, threaded=True)
    finally:
        pipeline.stop()
