FROM node:22-slim AS builder
WORKDIR /app
COPY package.json tsconfig.json ./
COPY packages/shared ./packages/shared
COPY packages/agent ./packages/agent
RUN npm install --workspace=packages/shared --workspace=packages/agent
RUN npm run build --workspace=packages/shared --workspace=packages/agent

FROM node:22-slim
RUN apt-get update && apt-get install -y git grep && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/packages/agent/dist ./dist
COPY --from=builder /app/packages/agent/package.json ./
COPY --from=builder /app/node_modules ./node_modules
ENV NODE_ENV=production
EXPOSE 3001
USER node
CMD ["node", "dist/index.js"]
