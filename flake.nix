{
  description = "cf-email-unwrapper: S/MIME email <-> A2A bridge for one agent";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          # openssl: all S/MIME work runs through the OpenSSL 3 CLI (env OPENSSL).
          packages = [ pkgs.python313 pkgs.uv pkgs.openssl pkgs.git ];
          shellHook = ''
            export OPENSSL=${pkgs.openssl.bin}/bin/openssl
            export UV_PYTHON=${pkgs.python313}/bin/python3.13
          '';
        };
      });
    };
}
