{ upstreamPath }:
let
  pin = builtins.fromJSON (builtins.readFile ./upstream.json);
  upstream = builtins.getFlake upstreamPath;
  pkgs = import upstream.inputs.nixpkgs {
    system = "x86_64-linux";
    overlays = [ upstream.overlays.default ];
  };
  rust = upstream.lib.rust.mkToolchain pkgs pin.rust_channel [
    "cargo"
    "llvm-tools-preview"
    "rustc"
    "rustc-dev"
  ];
in
assert builtins.hashFile "sha256" (upstreamPath + "/flake.lock") == pin.flake_lock_sha256;
pkgs.mkShellNoCC {
  packages = [
    upstream.packages.x86_64-linux.frontend
    rust
    (upstream.lib.mkDuneWrapper pkgs upstream.packages.x86_64-linux.refinedrust)
    pkgs.gcc
  ];
  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ rust ];
}
