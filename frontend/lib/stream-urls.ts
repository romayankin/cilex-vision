const GO2RTC_PORT = 1984;

export function getStreamUrls(cameraId: string) {
  const host = typeof window !== "undefined" ? window.location.hostname : "localhost";
  const base = `http://${host}:${GO2RTC_PORT}`;
  return {
    mse_url: `${base}/api/stream.mp4?src=${cameraId}`,
    hls_url: `${base}/api/stream.m3u8?src=${cameraId}`,
    snapshot_url: `${base}/api/frame.jpeg?src=${cameraId}`,
  };
}
