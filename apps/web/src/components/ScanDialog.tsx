import Close from "@mui/icons-material/Close";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Dialog from "@mui/material/Dialog";
import IconButton from "@mui/material/IconButton";
import Typography from "@mui/material/Typography";
import { useEffect, useRef, useState } from "react";
import { decodeCanvas } from "../lib/qrImage";

const SCAN_INTERVAL_MS = 200;

function cameraError(error: unknown): string {
  const name = (error as { name?: string } | null)?.name;
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Camera access is blocked. Allow it in your browser settings, or upload a photo of the QR instead.";
  }
  if (name === "NotFoundError") return "No camera found. Upload a photo of the QR instead.";
  return "The camera couldn't start. Upload a photo of the QR instead.";
}

interface Props {
  open: boolean;
  onClose: () => void;
  onResult: (text: string) => void;
}

export default function ScanDialog({ open, onClose, onResult }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [videoReady, setVideoReady] = useState(false);

  // The video element mounts with the dialog, so wait for its ref before starting the camera.
  useEffect(() => {
    if (!open || !videoReady) return;
    let stopped = false;
    let stream: MediaStream | null = null;
    let timer: number | undefined;
    const canvas = document.createElement("canvas");
    setError(null);

    const scan = async () => {
      const video = videoRef.current;
      if (stopped || !video) return;
      if (video.readyState >= video.HAVE_ENOUGH_DATA && video.videoWidth > 0) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext("2d", { willReadFrequently: true })?.drawImage(video, 0, 0);
        const text = await decodeCanvas(canvas).catch(() => null);
        if (text && !stopped) {
          stopped = true;
          onResult(text);
          return;
        }
      }
      if (!stopped) timer = window.setTimeout(() => void scan(), SCAN_INTERVAL_MS);
    };

    navigator.mediaDevices
      ?.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(async (media) => {
        if (stopped) {
          media.getTracks().forEach((track) => track.stop());
          return;
        }
        stream = media;
        const video = videoRef.current;
        if (!video) return;
        video.srcObject = media;
        await video.play();
        void scan();
      })
      .catch((e) => {
        if (!stopped) setError(cameraError(e));
      }) ?? setError("This browser can't use the camera. Upload a photo of the QR instead.");

    return () => {
      stopped = true;
      window.clearTimeout(timer);
      stream?.getTracks().forEach((track) => track.stop());
    };
  }, [open, videoReady, onResult]);

  return (
    <Dialog fullScreen open={open} onClose={onClose} slotProps={{ transition: { onExited: () => setVideoReady(false) } }}>
      <Box sx={{ position: "relative", flexGrow: 1, bgcolor: "#000", display: "grid", placeItems: "center", overflow: "hidden" }}>
        <video
          ref={(element) => {
            videoRef.current = element;
            if (element) setVideoReady(true);
          }}
          muted
          playsInline
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover" }}
        />
        <Box aria-hidden sx={{ position: "relative", width: "min(70vw, 300px)", aspectRatio: "1", border: "3px solid #fff", borderRadius: 4, boxShadow: "0 0 0 100vmax rgba(0,0,0,.45)" }} />
        <IconButton
          onClick={onClose}
          aria-label="Close scanner"
          sx={{ position: "absolute", top: "calc(env(safe-area-inset-top) + 8px)", left: 8, color: "#fff" }}
        >
          <Close />
        </IconButton>
        <Box sx={{ position: "absolute", bottom: "calc(env(safe-area-inset-bottom) + 32px)", left: 16, right: 16, textAlign: "center" }}>
          {error ? <Alert severity="warning">{error}</Alert> : <Typography sx={{ color: "#fff" }}>Point your camera at a UPI QR code</Typography>}
        </Box>
      </Box>
    </Dialog>
  );
}
