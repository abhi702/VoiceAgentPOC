FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY clinic-dashboard-nextjs/package*.json ./
RUN npm install --no-audit --no-fund
COPY clinic-dashboard-nextjs/ ./
ARG NEXT_PUBLIC_ORCHESTRATOR_URL=http://localhost:8000
ARG NEXT_PUBLIC_NESTJS_API_URL=http://localhost:3001
ENV NEXT_PUBLIC_ORCHESTRATOR_URL=$NEXT_PUBLIC_ORCHESTRATOR_URL
ENV NEXT_PUBLIC_NESTJS_API_URL=$NEXT_PUBLIC_NESTJS_API_URL
RUN npm run build

FROM node:22-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
