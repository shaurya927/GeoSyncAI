/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_CARTO_BASEMAP_KEY?: string
}
interface ImportMeta { readonly env: ImportMetaEnv }
