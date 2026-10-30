{
  description = "S/MIME email <-> A2A bridge for one agent (cf-email-gateway WebSocket)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
          ]
        )
      );
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          venv = pythonSets.${system}.mkVirtualEnv "cf-email-unwrapper-env" workspace.deps.default;
        in
        {
          # Stand-in A2A agent for testing a guardian.
          echo-agent = venv;
          default = pkgs.writeShellApplication {
            name = "cf-email-unwrapper";
            text = ''
              export OPENSSL="''${OPENSSL:-${lib.getExe pkgs.openssl}}"
              exec ${venv}/bin/cf-email-unwrapper "$@"
            '';
          };
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${system}.default;
        };
        echo-agent = {
          type = "app";
          program = "${self.packages.${system}.echo-agent}/bin/cf-email-echo-agent";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            # openssl: all S/MIME work runs through the OpenSSL 3 CLI (env OPENSSL).
            packages = [
              pkgs.python313
              pkgs.uv
              pkgs.openssl
              pkgs.git
            ];
            shellHook = ''
              export OPENSSL=${lib.getExe pkgs.openssl}
              export UV_PYTHON=${pkgs.python313}/bin/python3.13
            '';
          };
        }
      );
    };
}
