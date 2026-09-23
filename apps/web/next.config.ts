import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Opening the dev UI from another device on the LAN (or via this machine's
  // own LAN IP) otherwise gets "Blocked cross-origin request to Next.js dev
  // resource /_next/hmr", which breaks HMR. Dev-only; production builds
  // ignore this.
  allowedDevOrigins: ["192.168.0.33", "localhost", "127.0.0.1"],
};

export default nextConfig;
