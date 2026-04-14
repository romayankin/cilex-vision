"use client";

import { useEffect, useRef } from "react";

interface WebRtcPlayerProps {
  src: string;
  go2rtcHost: string;
  go2rtcPort?: number;
  className?: string;
  onError?: () => void;
  onPlaying?: () => void;
}

/**
 * Minimal WebRTC viewer for go2rtc. Creates a receive-only peer connection,
 * POSTs the SDP offer to /api/webrtc?src=... (JSON-wrapped — the format
 * go2rtc's HTTP WebRTC endpoint currently accepts), applies the answer, and
 * binds the remote MediaStream to a <video>. Falls back via onError.
 */
export default function WebRtcPlayer({
  src,
  go2rtcHost,
  go2rtcPort = 1984,
  className = "",
  onError,
  onPlaying,
}: WebRtcPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    let cancelled = false;
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    pc.ontrack = (event) => {
      if (cancelled) return;
      if (videoRef.current && event.streams[0]) {
        videoRef.current.srcObject = event.streams[0];
        onPlaying?.();
      }
    };

    pc.oniceconnectionstatechange = () => {
      if (cancelled) return;
      if (
        pc.iceConnectionState === "failed" ||
        pc.iceConnectionState === "disconnected"
      ) {
        onError?.();
      }
    };

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });

    (async () => {
      try {
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        const url = `http://${go2rtcHost}:${go2rtcPort}/api/webrtc?src=${encodeURIComponent(src)}`;
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type: "offer", sdp: offer.sdp }),
        });
        if (!res.ok) {
          throw new Error(`WebRTC offer failed: ${res.status}`);
        }

        const text = await res.text();
        let answer: RTCSessionDescriptionInit;
        try {
          const parsed = JSON.parse(text);
          answer = { type: parsed.type ?? "answer", sdp: parsed.sdp };
        } catch {
          answer = { type: "answer", sdp: text };
        }
        if (cancelled) return;
        await pc.setRemoteDescription(new RTCSessionDescription(answer));
      } catch (err) {
        console.error("WebRTC setup failed:", err);
        if (!cancelled) onError?.();
      }
    })();

    return () => {
      cancelled = true;
      pc.close();
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
    };
  }, [src, go2rtcHost, go2rtcPort, onError, onPlaying]);

  return (
    <video
      ref={videoRef}
      autoPlay
      muted
      playsInline
      className={className}
    />
  );
}
