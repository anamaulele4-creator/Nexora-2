# Frontend Next.js (em produção vai para a Vercel; esta imagem serve para correr tudo localmente)
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
ARG API_URL=http://api:8000
ENV API_URL=$API_URL NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app ./
USER node
EXPOSE 3000
CMD ["sh", "-c", "exec npx next start -H 0.0.0.0 -p ${PORT:-3000}"]
