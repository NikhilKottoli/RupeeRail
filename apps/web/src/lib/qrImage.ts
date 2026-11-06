/** Reads QR codes from images and camera frames, using the browser's detector when it has one. */

interface DetectorLike {
  detect(source: CanvasImageSource): Promise<{ rawValue: string }[]>;
}

declare global {
  interface Window {
    BarcodeDetector?: new (options: { formats: string[] }) => DetectorLike;
  }
}

let detector: DetectorLike | null | undefined;

function nativeDetector(): DetectorLike | null {
  if (detector === undefined) {
    try {
      detector = window.BarcodeDetector ? new window.BarcodeDetector({ formats: ["qr_code"] }) : null;
    } catch {
      detector = null;
    }
  }
  return detector;
}

/** Decodes whatever is drawn on the canvas. Returns null when no QR code is found. */
export async function decodeCanvas(canvas: HTMLCanvasElement): Promise<string | null> {
  const native = nativeDetector();
  if (native) {
    try {
      const codes = await native.detect(canvas);
      if (codes[0]?.rawValue) return codes[0].rawValue;
    } catch {
      // Some Android browsers expose the API but fail at runtime, so fall through to jsQR.
    }
  }
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context || canvas.width === 0 || canvas.height === 0) return null;
  const { default: jsQR } = await import("jsqr");
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  return jsQR(image.data, image.width, image.height)?.data || null;
}

const MAX_IMAGE_SIDE = 1600;

export async function decodeImageFile(file: File): Promise<string | null> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return decodeCanvas(canvas);
}
