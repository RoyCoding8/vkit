# Console assets

The local console uses official Material Web components, Material 3 color roles
generated from vkit's blue, and self-hosted Roboto. It can run selected checks,
cancel active runs, and invoke installed computations. Computation forms use
each operation's input schema, and results retain their status and scope.
Running registered checks updates gate evidence. Most computations return a separate result;
`check_release_artifact` runs an accepted build check and records its evidence.

Built assets are checked into `src/vkit/console-assets` and ship in the Python
package. Running the console needs neither Node nor an internet connection.
Console preferences are saved under the repository's Git common directory in
`vkit/config.json`, usually `.git/vkit/config.json`.
Launch defaults use the user config directory's `vkit/config.json`, or the file selected by
`VKIT_CONFIG`. **Setup & tools** shows both paths and edits each file separately.
**Project settings > Show tools** opens category switches, individual controls, and agent category scope.
MCP exposure can be Compact or Named. The **Computations** picker uses the same searchable, category-filtered dialog.

To rebuild from the pinned dependencies:

```powershell
npm ci --prefix tools/console
npm run build --prefix tools/console
```

`icons.svg` contains selected official Google Material Icons, pinned to the
revision in `icons.mjs`. Regenerate it with `node tools/console/icons.mjs`
(requires internet), then rebuild. Material Web, its color utilities, and the
icons use Apache 2.0; Roboto uses the SIL Open Font License. The generated
assets include their license texts.

References: https://material-web.dev/ and
https://github.com/google/material-design-icons.
