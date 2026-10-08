import { writeFile } from "node:fs/promises";

const revision = "737e3324305806514d7909874fa1818ae1808232";
const icons = {
  dashboard: "action/dashboard",
  checks: "action/fact_check",
  history: "action/history",
  extension: "action/extension",
  features: "action/view_module",
  proposals: "action/lightbulb_outline",
  settings: "action/settings",
  collapse: "navigation/menu_open",
  menu: "navigation/menu",
  refresh: "navigation/refresh",
  dark: "image/brightness_2",
  light: "image/wb_sunny",
  close: "navigation/close",
  search: "action/search",
  check: "action/check_circle",
  error: "alert/error_outline",
  schedule: "action/schedule",
  info: "action/info_outline",
  science: "social/science",
  code: "action/code",
  rule: "action/rule",
  security: "hardware/security",
  copy: "content/content_copy",
  expand: "navigation/expand_more",
};
const downloaded = await Promise.allSettled(
  Object.entries(icons).map(async ([id, path]) => {
    const response = await fetch(
      `https://raw.githubusercontent.com/google/material-design-icons/${revision}/src/${path}/materialicons/24px.svg`,
    );
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
    const svg = await response.text();
    const content = svg
      .replace(/^.*?<svg[^>]*>/s, "")
      .replace(/<\/svg>\s*$/, "")
      .replace(/fill="(?:#000000|black)"/g, "");
    return `<symbol id="${id}" viewBox="0 0 24 24">${content}</symbol>`;
  }),
);
const errors = downloaded.filter((result) => result.status === "rejected");
if (errors.length)
  throw new AggregateError(
    errors.map((result) => result.reason),
    "Icon download failed",
  );
const symbols = downloaded.map((result) => result.value);
await writeFile(
  new URL("icons.svg", import.meta.url),
  `<!-- Google Material Icons, Apache-2.0; revision ${revision} -->\n<svg xmlns="http://www.w3.org/2000/svg">\n${symbols.join("\n")}\n</svg>\n`,
);
console.log(
  `Downloaded ${symbols.length} official Material icons at ${revision}.`,
);
