import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const backend = env.TERRARIUM_GUI_BACKEND ?? "http://127.0.0.1:8765";

  return {
    plugins: [react()],
    build: {
      outDir: "../../src/terrarium_gui/static",
      emptyOutDir: true,
      sourcemap: false,
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: backend,
          changeOrigin: false,
        },
      },
    },
  };
});
