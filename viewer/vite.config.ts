import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { cpSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
const root = fileURLToPath(new URL("..", import.meta.url));
export default defineConfig({
  base: "./",
  plugins: [
    react(),
    {
      name: "synthetic-example",
      closeBundle() {
        cpSync(
          resolve(root, "examples/synthetic-bundle"),
          resolve(root, "viewer/dist/examples/synthetic-bundle"),
          { recursive: true },
        );
      },
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          if (req.url?.startsWith("/examples/"))
            req.url = `/@fs/${root}${req.url}`;
          next();
        });
      },
    },
  ],
  server: { host: "127.0.0.1", fs: { allow: [root] } },
  preview: { host: "127.0.0.1" },
});
