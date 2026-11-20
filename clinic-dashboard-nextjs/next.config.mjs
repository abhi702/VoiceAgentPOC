import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

const envPath = fileURLToPath(new URL("../.env", import.meta.url));
if (existsSync(envPath)) process.loadEnvFile(envPath);

export default {
  output: "standalone",
  env: {
    NEXT_PUBLIC_ORCHESTRATOR_URL: process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ?? "http://localhost:8000",
    NEXT_PUBLIC_NESTJS_API_URL: process.env.NEXT_PUBLIC_NESTJS_API_URL ?? "http://localhost:3001",
  },
  turbopack: { root: fileURLToPath(new URL(".", import.meta.url)) },
};
