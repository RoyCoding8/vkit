{ upstreamPath }:
let
  pin = builtins.fromJSON (builtins.readFile ./upstream.json);
  upstream = builtins.getFlake upstreamPath;
  pkgs = import upstream.inputs.nixpkgs {
    system = "x86_64-linux";
    overlays = [ upstream.overlays.default ];
  };
  rustConfig = (builtins.fromTOML (builtins.readFile (upstreamPath + "/rr_frontend/rust-toolchain.toml"))).toolchain;
  rust = upstream.lib.rust.mkToolchain pkgs rustConfig.channel rustConfig.components;
  frontend = upstream.packages.x86_64-linux.frontend.overrideAttrs (old: {
    patches = (old.patches or []) ++ [ ./frontend-inventory.patch ];
  });
in
assert builtins.hashFile "sha256" (upstreamPath + "/flake.lock") == pin.flake_lock_sha256;
pkgs.mkShellNoCC {
  packages = [
    frontend
    rust
    (upstream.lib.mkDuneWrapper pkgs upstream.packages.x86_64-linux.refinedrust)
    pkgs.gcc
  ];
  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ rust ];
}
