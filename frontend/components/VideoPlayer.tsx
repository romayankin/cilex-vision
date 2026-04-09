"use client";

import { useEffect, useRef } from "react";

interface VideoPlayerProps {
  src: string | null;
  poster?: string;
  autoPlay?: boolean;
}

export default function VideoPlayer({ src, poster, autoPlay = false }: VideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<{ destroy: () => void } | null>(null);

  useEffect(() => {
    if (!src || !videoRef.current) return;

    const video = videoRef.current;

    // Check if HLS stream
    if (src.endsWith(".m3u8")) {
      // Dynamic import of hls.js to avoid SSR issues
      import("hls.js").then(({ default: Hls }) => {
        if (Hls.isSupported()) {
          const hls = new Hls();
          hls.loadSource(src);
          hls.attachMedia(video);
          hlsRef.current = hls;
          if (autoPlay) {
            hls.on(Hls.Events.MANIFEST_PARSED, () => {
              video.play().catch(() => {});
            });
          }
        } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
          // Native HLS support (Safari)
          video.src = src;
          if (autoPlay) video.play().catch(() => {});
        }
      });
    } else {
      // Direct video URL (MP4 signed MinIO URL)
      video.src = src;
      if (autoPlay) video.play().catch(() => {});
    }

    return () => {
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
    };
  }, [src, autoPlay]);

  if (!src) {
    return (
      <div className="bg-gray-900 rounded-lg flex items-center justify-center h-64">
        <span className="text-gray-500">No video available</span>
      </div>
    );
  }

  return (
    <video
      ref={videoRef}
      poster={poster}
      controls
      className="w-full rounded-lg bg-black"
      playsInline
    />
  );
}
