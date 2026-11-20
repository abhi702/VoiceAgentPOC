FROM node:22-bookworm-slim
WORKDIR /app
COPY clinic-backend-nestjs/package*.json ./
RUN npm install --no-audit --no-fund
COPY clinic-backend-nestjs/tsconfig.json ./
COPY clinic-backend-nestjs/src ./src
RUN npm run build
EXPOSE 3001
CMD ["npm", "start"]
