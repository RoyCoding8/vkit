# Console assets

The read-only console uses official Material Web components, Material 3 color
roles generated from vkit's blue, and self-hosted Roboto. Built assets are
checked into `src/vkit/console-assets` and ship in the Python package. Running
the console needs neither Node nor an internet connection.

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
