import "reflect-metadata";
import { config } from "dotenv";
import { resolve } from "node:path";
import { NestFactory } from "@nestjs/core";
import { ValidationPipe } from "@nestjs/common";
import { AppModule } from "./app.module";

config({ path: resolve(__dirname, "../../.env") });

async function main(): Promise<void> {
  for (const key of ["MYSQL_PASSWORD", "CLINIC_API_TOKEN"]) {
    if (!process.env[key]) throw new Error(`Missing required environment variable: ${key}`);
  }
  const app = await NestFactory.create(AppModule);
  app.enableCors({ origin: process.env.ALLOWED_ORIGIN ?? "http://localhost:3000" });
  app.useGlobalPipes(new ValidationPipe({ whitelist: true, forbidNonWhitelisted: true, transform: true }));
  app.enableShutdownHooks();
  await app.listen(3001, process.env.MYSQL_HOST === "mysql" ? "0.0.0.0" : "127.0.0.1");
}

void main().catch((error: unknown) => {
  console.error("Clinic startup failed", error);
  process.exitCode = 1;
});
