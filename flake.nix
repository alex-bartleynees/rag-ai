{
  description = "rag-ai service and development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, uv2nix, pyproject-nix, pyproject-build-systems }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
        pythonSet = (pkgs.callPackage pyproject-nix.build.packages {
          python = pkgs.python312;
        }).overrideScope (pkgs.lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
          (final: prev: {
            rtfm-for-me = prev.rtfm-for-me.overrideAttrs (old: {
              nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem { hatchling = []; };
            });
          })
        ]);
        # Use the locked binary adapter so libpq is available under systemd's minimal PATH.
        runtime = pythonSet.mkVirtualEnv "rag-ai-runtime" (workspace.deps.default // {
          psycopg-binary = [];
        });
      in
      {
        packages = rec {
          default = rag-ai;
          rag-ai = pkgs.writeShellApplication {
            name = "rag-ai";
            text = ''
              exec ${runtime}/bin/uvicorn app.main:app --app-dir "${runtime}/${pkgs.python312.sitePackages}" \
                --host 127.0.0.1 --port "''${RAG_AI_PORT:-8000}" --workers 1 --lifespan on
            '';
          };
        };
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            # Python
            python312
            python312Packages.pip
            uv                        # fast venv/dep management

            # Database tooling
            postgresql_17             # gives you psql client

            # Infra
            docker-compose

            # Useful dev tools
            just                      # taskrunner, good companion to a Justfile
            jq
            curl
          ];

          shellHook = ''
            echo "rag-ai dev shell"

            # Create a local venv if one doesn't exist
            if [ ! -d .venv ]; then
              echo "Creating .venv..."
              uv venv .venv
            fi

            source .venv/bin/activate

            # Ensure .env exists so docker-compose doesn't complain
            if [ ! -f .env ]; then
              echo "Warning: no .env file found. See the README for local configuration."
            fi
          '';

          # Helps psycopg / other C-ext packages find libs at build time
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
            pkgs.libpq
            pkgs.openssl
          ];
        };
      }
    ) // {
      homeManagerModules.default = import ./deploy/home-manager.nix { inherit self; };
      nixosModules.default = import ./deploy/nixos.nix;
    };
}
