// esbuild で src/ を dist/ へバンドルし、manifest と静的 HTML をコピーする。
import { build } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });

const common = {
  bundle: true,
  format: "esm",
  target: "chrome120",
  sourcemap: false,
  minify: false,
  logLevel: "info",
};

await build({ ...common, entryPoints: ["src/background.ts"], outfile: "dist/background.js" });
await build({ ...common, entryPoints: ["src/offscreen.ts"], outfile: "dist/offscreen.js" });
await build({ ...common, entryPoints: ["src/popup.ts"], outfile: "dist/popup.js" });
// AudioWorklet は module script として読み込むため単独でバンドルする
await build({ ...common, entryPoints: ["src/worklet.ts"], outfile: "dist/worklet.js" });

cpSync("manifest.json", "dist/manifest.json");
cpSync("src/offscreen.html", "dist/offscreen.html");
cpSync("src/popup.html", "dist/popup.html");
cpSync("icons", "dist/icons", { recursive: true });
console.log("dist/ を生成しました");
