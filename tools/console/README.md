# Console assets

This directory contains the frontend build sources. For running checks and managing workspaces, see the [console guide](../../docs/console.md).

The interface uses Material Web components, Material 3 color roles, and self-hosted Roboto fonts. Generated assets live in `src/vkit/console-assets` and ship in the Python package. Running the console needs neither Node nor an internet connection.

## Rebuild the assets

Run from the repository root:

```powershell
npm ci --prefix tools/console
npm run build --prefix tools/console
```

To refresh the pinned icons:

1. Run `node tools/console/icons.mjs`. This requires internet access.
2. Rebuild the console assets.

## Sources and licenses

| Asset | Source or pin | License |
| --- | --- | --- |
| Material Web and color utilities | [Material Web](https://material-web.dev/) | Apache 2.0. |
| `icons.svg` | Selected [Google Material Icons](https://github.com/google/material-design-icons), pinned to the revision in `icons.mjs`. | Apache 2.0. |
| Roboto | Self-hosted font files. | SIL Open Font License. |

Generated assets include the license texts.
