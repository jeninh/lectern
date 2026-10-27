// The "everyone" docs with their Markdown, bundled by vite.config.ts (docsText).
declare module "virtual:lectern-docs" {
  const text: import("./lib/docs").DocText;
  export default text;
}
