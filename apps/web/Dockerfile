FROM node:20-slim AS build
WORKDIR /app
ARG NEXT_PUBLIC_WS_URL
ENV NEXT_PUBLIC_WS_URL=${NEXT_PUBLIC_WS_URL}
# Port the browser reaches the API on (the WebSocket and the history endpoint
# both derive their host from the page's own location, so only the port needs
# baking in). Must match the server service's published port.
ARG NEXT_PUBLIC_WS_PORT=8100
ENV NEXT_PUBLIC_WS_PORT=${NEXT_PUBLIC_WS_PORT}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "start"]
