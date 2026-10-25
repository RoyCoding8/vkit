{ upstreamPath, modelOnly ? false }:
let
  upstream = builtins.getFlake upstreamPath;
  pkgs = import upstream.inputs.nixpkgs {
    system = "x86_64-linux";
    overlays = [ upstream.overlays.default ];
  };
in
map
  (package: {
    name = package.pname;
    revision = package.version;
    hash = package.src.outputHash;
    path = package.src.outPath;
    url = package.src.url or (builtins.head package.src.urls);
  })
  (with pkgs.rocqPackages; if modelOnly then [ stdpp ] else [
    stdpp
    iris
    iris-contrib
    lambda-rust
  ])
