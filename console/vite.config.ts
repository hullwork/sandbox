import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Development connects directly to the locally exposed Control Plane. The
// production Console proxies through its own Nginx layer, so only development
// optionally injects Authorization. Developers export the token themselves;
// the default is no injection, making the expected 401 visible instead of
// suggesting that the Console itself is broken.
const controlPlaneTarget = process.env.VITE_DEV_CONTROL_PLANE_TARGET ?? "http://127.0.0.1:18080";
const devControlPlaneToken = process.env.VITE_DEV_CONTROL_PLANE_TOKEN ?? "";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 3100,
    proxy: {
      "/v1": {
        target: controlPlaneTarget,
        changeOrigin: true,
        headers: devControlPlaneToken
          ? { Authorization: `Bearer ${devControlPlaneToken}` }
          : undefined,
      },
      "/healthz": {
        target: controlPlaneTarget,
        changeOrigin: true,
      },
    },
  },
});
