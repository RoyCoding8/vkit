# Console assets

## Runtime behavior

| Area | Behavior |
| --- | --- |
| Interface | Official Material Web components, Material 3 color roles generated from VKit's blue, and self-hosted Roboto. |
| Jobs | Run registered checks, show durable runs and logs, and cancel active runs. |
| Gate evidence | Updates only when the accepted checker's recorded inputs and outcome match. |
| Packaged assets | Checked into `src/vkit/console-assets` and included in the Python package. |
| Runtime requirements | No Node or internet connection needed to run the console. |

## Settings locations

| Settings | Location |
| --- | --- |
| Repository preferences | `<git common directory>/vkit/config.json`, usually `.git/vkit/config.json`. |
| Launch defaults | User config directory's `vkit/config.json`, or the file selected by `VKIT_CONFIG`. |
| File controls | **Setup & tools** shows and edits the two files separately. |
| Tool controls | **Project settings > Show tools** provides category switches, individual controls, and agent category scope. |
| MCP exposure | Compact or Named. |

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
