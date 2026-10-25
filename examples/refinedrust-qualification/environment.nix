{ upstreamPath, modelOnly ? false }:
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
    patches = (old.patches or []) ++ [ ./frontend.patch ];
  });
  modelEnvironment = upstream.lib.mkDuneWrapper pkgs pkgs.rocqPackages.stdpp;
  libraries = upstream.packages.x86_64-linux;
  stdlib = rec {
    closures = libraries.stdlib-closures.overrideAttrs (_: {
      propagatedBuildInputs = [ libraries.refinedrust ];
    });
    sized = libraries.stdlib-sized.overrideAttrs (_: {
      propagatedBuildInputs = [ libraries.refinedrust ];
    });
    clone = libraries.stdlib-clone.overrideAttrs (_: {
      propagatedBuildInputs = [ libraries.refinedrust sized ];
    });
    result = libraries.stdlib-result.overrideAttrs (_: {
      propagatedBuildInputs = [ libraries.refinedrust closures clone ];
    });
    option = libraries.stdlib-option.overrideAttrs (_: {
      propagatedBuildInputs = [ libraries.refinedrust closures clone result ];
    });
  };
  verificationEnvironment = upstream.lib.mkDuneWrapper pkgs stdlib.option;
in
assert builtins.hashFile "sha256" (upstreamPath + "/flake.lock") == pin.flake_lock_sha256;
pkgs.mkShellNoCC ({
  packages = if modelOnly then [ modelEnvironment ] else [
    frontend
    rust
    verificationEnvironment
    pkgs.gcc
  ];
} // pkgs.lib.optionalAttrs modelOnly {
  ROCQPATH = "${modelEnvironment}/lib/coq/${pkgs.rocqPackages.rocq-core.rocq-version}/user-contrib";
} // pkgs.lib.optionalAttrs (!modelOnly) {
  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ rust ];
  RR_NIX_STDLIB = "${verificationEnvironment}/share";
})
