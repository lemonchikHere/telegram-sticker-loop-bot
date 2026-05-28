import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { createRequire } from "node:module";
import { chromium } from "playwright-core";

const require = createRequire(import.meta.url);

function readAnimation(inputPath) {
  const raw = fs.readFileSync(inputPath);
  const json = inputPath.endsWith(".tgs") || raw[0] === 0x1f && raw[1] === 0x8b
    ? zlib.gunzipSync(raw).toString("utf8")
    : raw.toString("utf8");
  return JSON.parse(json);
}

function chromePath() {
  if (process.env.CHROME_PATH) {
    return process.env.CHROME_PATH;
  }

  const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (fs.existsSync(macChrome)) {
    return macChrome;
  }

  return undefined;
}

function parseArgs(argv) {
  const args = new Map();
  for (let i = 2; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`Bad argument near ${key ?? "<end>"}`);
    }
    args.set(key.slice(2), value);
  }

  return {
    input: args.get("input"),
    outDir: args.get("out-dir"),
    width: Number(args.get("width") ?? 512),
    height: Number(args.get("height") ?? 512),
    fps: Number(args.get("fps") ?? 30),
    maxSeconds: Number(args.get("max-seconds") ?? 6),
  };
}

async function main() {
  const options = parseArgs(process.argv);
  if (!options.input || !options.outDir) {
    throw new Error("Usage: node src/render_lottie.mjs --input in.tgs --out-dir frames --width 512 --height 512 --fps 30");
  }

  fs.mkdirSync(options.outDir, { recursive: true });

  const animationData = readAnimation(options.input);
  const sourceFps = Number(animationData.fr || 60);
  const inPoint = Number(animationData.ip || 0);
  const outPoint = Number(animationData.op || sourceFps * 3);
  const rawDuration = Math.max(1 / sourceFps, (outPoint - inPoint) / sourceFps);
  const duration = Math.min(rawDuration, options.maxSeconds);
  const frameCount = Math.max(1, Math.ceil(duration * options.fps));
  const lottiePath = require.resolve("lottie-web/build/player/lottie.min.js");
  const lottieScript = fs.readFileSync(lottiePath, "utf8");

  const launchOptions = { headless: true, args: ["--disable-gpu", "--no-sandbox"] };
  const executablePath = chromePath();
  if (executablePath) {
    launchOptions.executablePath = executablePath;
  }

  const browser = await chromium.launch(launchOptions);
  const page = await browser.newPage({
    viewport: { width: options.width, height: options.height },
    deviceScaleFactor: 1,
  });

  page.on("console", (message) => {
    if (message.type() === "error") {
      console.error(message.text());
    }
  });

  await page.setContent(`<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      html, body {
        width: ${options.width}px;
        height: ${options.height}px;
        margin: 0;
        background: transparent;
        overflow: hidden;
      }

      #stage {
        width: ${options.width}px;
        height: ${options.height}px;
        background: transparent;
      }

      svg, canvas {
        display: block;
      }
    </style>
  </head>
  <body>
    <div id="stage"></div>
    <script>${lottieScript}</script>
  </body>
</html>`);

  await page.evaluate((data) => new Promise((resolve, reject) => {
    try {
      window.__animation = window.lottie.loadAnimation({
        container: document.getElementById("stage"),
        renderer: "svg",
        loop: true,
        autoplay: false,
        animationData: data,
        rendererSettings: {
          preserveAspectRatio: "xMidYMid meet",
          progressiveLoad: false,
          hideOnTransparent: true,
        },
      });
      window.__animation.setSubframe(false);
      window.__animation.addEventListener("DOMLoaded", resolve);
      window.__animation.addEventListener("data_failed", () => reject(new Error("Lottie data failed")));
      setTimeout(resolve, 2000);
    } catch (error) {
      reject(error);
    }
  }), animationData);

  for (let index = 0; index < frameCount; index += 1) {
    const frame = inPoint + index * sourceFps / options.fps;
    await page.evaluate((targetFrame) => {
      window.__animation.goToAndStop(targetFrame, true);
    }, frame);

    const fileName = `frame_${String(index + 1).padStart(5, "0")}.png`;
    await page.screenshot({
      path: path.join(options.outDir, fileName),
      omitBackground: true,
      clip: { x: 0, y: 0, width: options.width, height: options.height },
    });
  }

  await browser.close();

  fs.writeFileSync(path.join(options.outDir, "manifest.json"), JSON.stringify({
    fps: options.fps,
    source_fps: sourceFps,
    frame_count: frameCount,
    duration,
    width: options.width,
    height: options.height,
  }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});

