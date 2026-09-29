import type { NextConfig } from "next";
const config: NextConfig = { output: "standalone", outputFileTracingRoot: process.cwd(), poweredByHeader: false, devIndicators: false, experimental: { cpus: 2 } };
export default config;
