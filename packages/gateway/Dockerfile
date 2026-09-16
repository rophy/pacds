FROM node:22-slim AS builder
WORKDIR /app
COPY package.json tsconfig.json ./
COPY packages/shared ./packages/shared
COPY packages/gateway ./packages/gateway
RUN npm install --workspace=packages/shared --workspace=packages/gateway
RUN npm run build --workspace=packages/shared --workspace=packages/gateway

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/packages/gateway/dist ./dist
COPY --from=builder /app/packages/gateway/package.json ./
COPY --from=builder /app/node_modules ./node_modules
ENV NODE_ENV=production
EXPOSE 3000
USER node
CMD ["node", "dist/main.js"]
