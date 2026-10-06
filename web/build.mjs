// Bundles the React app into ../app/static so the Python app serves it (one CF app, one origin).
//   node build.mjs          production build (minified)
//   node build.mjs --watch  rebuild on change (use with `make dev`)
import * as esbuild from "esbuild";
import { copyFileSync, mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(here, "../app/static");
const watch = process.argv.includes("--watch");

rmSync(outDir, { recursive: true, force: true });
mkdirSync(resolve(outDir, "assets"), { recursive: true });
copyFileSync(resolve(here, "index.html"), resolve(outDir, "index.html"));

const options = {
  entryPoints: [resolve(here, "src/main.tsx")],
  bundle: true,
  outdir: resolve(outDir, "assets"),
  entryNames: "app",
  format: "esm",
  target: ["es2022"],
  jsx: "automatic",
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  legalComments: "none",
  define: { "process.env.NODE_ENV": JSON.stringify(watch ? "development" : "production") },
  loader: { ".css": "css" },
  logLevel: "info",
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("Watching web/src for changes...");
} else {
  await esbuild.build(options);
}
