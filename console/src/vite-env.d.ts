/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_FORGE_API?: "mock" | "real";
  readonly VITE_FORGE_BASE_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
