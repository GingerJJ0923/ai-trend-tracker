import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = process.cwd();
const source = resolve(root, "public", "prototype");
const dist = resolve(root, "dist");
const client = resolve(dist, "client");
const server = resolve(dist, "server");

await rm(dist, { recursive: true, force: true });
await mkdir(resolve(client, "prototype"), { recursive: true });
await mkdir(server, { recursive: true });
await mkdir(resolve(dist, ".openai"), { recursive: true });
await cp(source, resolve(client, "prototype"), { recursive: true });

const worker = `export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/index.html") {
      url.pathname = "/prototype/index.html";
    }
    return env.ASSETS.fetch(new Request(url, request));
  },
};
`;

await writeFile(resolve(server, "index.js"), worker, "utf8");
await cp(
  resolve(root, ".openai", "hosting.json"),
  resolve(dist, ".openai", "hosting.json"),
);

const html = await readFile(resolve(client, "prototype", "index.html"), "utf8");
if (!html.includes("Signal Radar") || !html.includes("今日重点情报")) {
  throw new Error("Signal Radar prototype content is incomplete");
}

console.log("Signal Radar static build is ready.");
