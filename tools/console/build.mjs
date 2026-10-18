import { build } from "esbuild";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
// The pinned package's root export contains an extensionless Node import.
import {
  argbFromHex,
  hexFromArgb,
} from "./node_modules/@material/material-color-utilities/utils/string_utils.js";
import { themeFromSourceColor } from "./node_modules/@material/material-color-utilities/utils/theme_utils.js";

const output = new URL("../../src/vkit/console-assets/", import.meta.url);
await mkdir(output, { recursive: true });
await build({
  entryPoints: [fileURLToPath(new URL("material.js", import.meta.url))],
  outfile: fileURLToPath(new URL("material.js", output)),
  bundle: true,
  minify: true,
  format: "esm",
  target: "es2022",
  legalComments: "linked",
});
await copyFile(
  new URL(
    "node_modules/@fontsource-variable/roboto/files/roboto-latin-wght-normal.woff2",
    import.meta.url,
  ),
  new URL("roboto.woff2", output),
);
await copyFile(
  new URL("../../docs/assets/logo.svg", import.meta.url),
  new URL("logo.svg", output),
);
await copyFile(
  new URL("icons.svg", import.meta.url),
  new URL("icons.svg", output),
);

const theme = themeFromSourceColor(argbFromHex("#2a78d6"));
const success = themeFromSourceColor(argbFromHex("#2b7657"));
const warning = themeFromSourceColor(argbFromHex("#8a6100"));
const css = [];
for (const mode of ["light", "dark"]) {
  const roles = theme.schemes[mode].toJSON();
  const tones =
    mode === "light" ? [98, 100, 96, 94, 92, 90] : [6, 4, 10, 12, 17, 22];
  [
    "surface",
    "surfaceContainerLowest",
    "surfaceContainerLow",
    "surfaceContainer",
    "surfaceContainerHigh",
    "surfaceContainerHighest",
  ].forEach((name, i) => {
    roles[name] = theme.palettes.neutral.tone(tones[i]);
  });
  const lines = Object.entries(roles).map(
    ([name, value]) =>
      `  --md-sys-color-${name.replace(/[A-Z]/g, (char) => "-" + char.toLowerCase())}: ${hexFromArgb(value)};`,
  );
  for (const [name, palette] of [
    ["success", success],
    ["warning", warning],
  ]) {
    const scheme = palette.schemes[mode].toJSON();
    lines.push(
      `  --${name}: ${hexFromArgb(scheme.primary)};`,
      `  --${name}-container: ${hexFromArgb(scheme.primaryContainer)};`,
      `  --on-${name}-container: ${hexFromArgb(scheme.onPrimaryContainer)};`,
    );
  }
  const selector =
    mode === "light"
      ? ':root, :root[data-theme="light"]'
      : ':root[data-theme="dark"]';
  css.push(`${selector} {\n  color-scheme: ${mode};\n${lines.join("\n")}\n}`);
  if (mode === "dark")
    css.push(
      `@media (prefers-color-scheme: dark) {\n:root:not([data-theme]) {\n  color-scheme: dark;\n${lines.join("\n")}\n}\n}`,
    );
}
await writeFile(new URL("theme.css", output), css.join("\n") + "\n");
const licenses = [
  ["Material Web", "node_modules/@material/web/LICENSE"],
  ["Google Material Icons (Copyright Google LLC)", "node_modules/@material/web/LICENSE"],
  [
    "Material color utilities",
    "node_modules/@material/material-color-utilities/LICENSE",
  ],
  ["Roboto", "node_modules/@fontsource-variable/roboto/LICENSE"],
];
await writeFile(
  new URL("LICENSES.txt", output),
  (
    await Promise.all(
      licenses.map(
        async ([name, path]) =>
          `${name}\n\n${await readFile(new URL(path, import.meta.url), "utf8")}`,
      ),
    )
  ).join("\n\n"),
);
console.log(
  "Built local Material Web components, Material 3 theme tokens, Roboto, and licenses.",
);
