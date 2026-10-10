// Pictures attached in the Ask Jarvis panel: validate them and stage them for the jarvis CLI.
//
// WHAT THIS DOES AND DOES NOT DO
// ------------------------------
// The browser sends each picture as base64 in the "ask" WebSocket message. This module only
// CHECKS and STAGES them; it never describes, stores or serves anything:
//
//   * how many (MAX_IMAGES) and how big each is (MAX_IMAGE_BYTES), measured on the encoded text
//     BEFORE it is decoded, so an oversized value is refused without allocating it;
//   * what each really is: the first bytes must be a png / jpeg / gif / webp signature. A name or
//     a MIME type the browser reported is never trusted, and the file this module writes gets its
//     extension from the signature, not from the name;
//   * the file name is only a label: path separators and control characters are stripped.
//
// The checked bytes go to a PRIVATE temp folder (0700) with a small manifest the CLI reads
// (JARVIS_ASK_IMAGES_FILE -> jarvis/cli.py `_load_panel_pictures` -> channels/media_in.prepare_files).
// The CLI is what reads them with the vision model and keeps a copy with the conversation
// (jarvis/conv_images.py). server.js removes the folder when the child exits, whatever the exit.
//
// The limits mirror jarvis/channels/media_in.py (MAX_IMAGES = 3, default max_image_mb = 8) and
// jarvis/conv_images.py (MAX_PER_MESSAGE = 3); tests/test_ask_images.py fails if they drift.

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export const MAX_IMAGES = 3;
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
// What the model is asked when a message is only pictures (the same wording the Discord
// gateway uses, jarvis/channels/discord_gateway.py), so the words are never an empty string.
export const ASK_IMAGE_PLACEHOLDER = "(sent a picture)";

const MAX_BASE64_CHARS = Math.ceil(MAX_IMAGE_BYTES / 3) * 4 + 4;
const MAX_NAME = 80;

export function sniffImageKind(buf) {
  if (!buf || buf.length < 12) return "";
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47 &&
      buf[4] === 0x0d && buf[5] === 0x0a && buf[6] === 0x1a && buf[7] === 0x0a) return "png";
  if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "jpg";
  const head6 = buf.subarray(0, 6).toString("latin1");
  if (head6 === "GIF87a" || head6 === "GIF89a") return "gif";
  if (buf.subarray(0, 4).toString("latin1") === "RIFF" &&
      buf.subarray(8, 12).toString("latin1") === "WEBP") return "webp";
  return "";
}

export function cleanName(name) {
  const base = String(name == null ? "" : name).split(/[\\/]/).pop() || "";
  const text = base.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, MAX_NAME);
  return text || "picture";
}

// raw is whatever the message carried in `images`. Returns { ok: true, images: [{ name, bytes, kind }] }
// (images may be empty when nothing was attached) or { ok: false, error } with a message fit to show.
export function parseAskImages(raw) {
  if (raw === undefined || raw === null) return { ok: true, images: [] };
  if (!Array.isArray(raw)) return { ok: false, error: "Those pictures weren't in a form I can read." };
  if (raw.length === 0) return { ok: true, images: [] };
  if (raw.length > MAX_IMAGES) {
    return { ok: false, error: `At most ${MAX_IMAGES} pictures per message.` };
  }
  const images = [];
  for (const item of raw) {
    if (!item || typeof item !== "object" || typeof item.data !== "string") {
      return { ok: false, error: "One of the pictures wasn't in a form I can read." };
    }
    const name = cleanName(item.name);
    if (item.data.length > MAX_BASE64_CHARS) {
      return { ok: false, error: `${name} is over the ${MAX_IMAGE_BYTES / 1048576} MB limit.` };
    }
    const bytes = Buffer.from(item.data, "base64");
    if (bytes.length === 0) return { ok: false, error: `${name} is empty.` };
    if (bytes.length > MAX_IMAGE_BYTES) {
      return { ok: false, error: `${name} is over the ${MAX_IMAGE_BYTES / 1048576} MB limit.` };
    }
    const kind = sniffImageKind(bytes);
    if (!kind) return { ok: false, error: `${name} isn't a PNG, JPEG, GIF or WebP picture.` };
    images.push({ name, bytes, kind });
  }
  return { ok: true, images };
}

// Writes the checked pictures and the manifest into a fresh private folder.
// `shown` is what the person typed (stored with the exchange so a reload can show the words
// without the picture-description note). Returns { dir, manifestPath }; throws on a disk error
// after removing whatever it had written.
export async function stageAskImages(images, shown, baseDir = os.tmpdir()) {
  const dir = await fs.mkdtemp(path.join(baseDir, "jarvis-askimg-"));
  try {
    const files = [];
    let i = 0;
    for (const img of images) {
      i += 1;
      const file = path.join(dir, `p${i}.${img.kind}`);
      await fs.writeFile(file, img.bytes, { mode: 0o600 });
      files.push({ path: file, name: img.name });
    }
    const manifestPath = path.join(dir, "manifest.json");
    await fs.writeFile(manifestPath, JSON.stringify({ files, shown: String(shown || "") }),
      { encoding: "utf8", mode: 0o600 });
    return { dir, manifestPath };
  } catch (err) {
    await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
    throw err;
  }
}
