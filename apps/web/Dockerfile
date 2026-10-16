FROM node:20-slim AS build
WORKDIR /app
# How the browser finds the server. The rules, and what each value does, are
# in apps/web/src/lib/urls.ts; Next bakes these into the bundle, so an image
# built with an absolute URL carries that address wherever it is run.
#
# Absolute overrides. Left blank, the address is derived from the page, which
# is what lets one image serve localhost and the LAN alike.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ARG NEXT_PUBLIC_WS_URL
ENV NEXT_PUBLIC_WS_URL=${NEXT_PUBLIC_WS_URL}
# Port the browser reaches the API on, for the split deployment. Must match
# the server service's published port.
ARG NEXT_PUBLIC_WS_PORT=8100
ENV NEXT_PUBLIC_WS_PORT=${NEXT_PUBLIC_WS_PORT}
# "same-origin" when the daemon serves the UI and the API together; anything
# else, including unset, keeps the split-port default.
ARG NEXT_PUBLIC_SERVER_MODE
ENV NEXT_PUBLIC_SERVER_MODE=${NEXT_PUBLIC_SERVER_MODE}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "start"]
