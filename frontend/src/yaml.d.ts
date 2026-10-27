// YAML files are parsed at build time (vite.config.ts) into their data.
declare module "*.yml" {
  const data: unknown;
  export default data;
}
