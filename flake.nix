{
  description = "mcp-silverbullet — Model Context Protocol bridge between SilverBullet and MCP clients";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

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
      uv2nix,
      pyproject-nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      # Per-system python sets. We pin to `pkgs.python313` to match the
      # Python version that resolved our `uv.lock` (CPython 3.13.15).
      # nixpkgs's default `python3` drifts — keeping it pinned avoids
      # the lockfile and the venv disagreeing on wheels.
      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python313;
          baseSet = pkgs.callPackage pyproject-nix.build.packages {
            inherit python;
          };
        in
        # A second `overrideScope` attaches `passthru.tests.pytest` to the
        # `mcp-silverbullet` package itself, so `checks.${system}.pytest`
        # exposes the test derivation exactly as the uv2nix testing
        # pattern documents it.
        baseSet.overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
            (final: prev: {
              mcp-silverbullet = prev.mcp-silverbullet.overrideAttrs (old: {
                passthru = old.passthru // {
                  tests =
                    let
                      # Venv containing the project + the `test` extra
                      # (pytest, pytest-asyncio, respx from
                      # [project.optional-dependencies] test).
                      testVenv = final.mkVirtualEnv "mcp-silverbullet-pytest-env" workspace.deps.all;
                    in
                    (old.passthru.tests or { })
                    // {
                      pytest = pkgs.stdenv.mkDerivation {
                        name = "${final.mcp-silverbullet.name}-pytest";
                        # `lib.cleanSource` strips gitignored files
                        # (.venv, __pycache__, .pytest_cache, ...). The
                        # test derivation's CWD is the repo root, so
                        # pytest finds tests/ via pyproject.toml's
                        # `testpaths = ["tests"]`.
                        src = lib.cleanSource ./.;
                        nativeBuildInputs = [ testVenv ];
                        dontConfigure = true;

                        buildPhase = ''
                          runHook preBuild
                          pytest
                          runHook postBuild
                        '';

                        installPhase = ''
                          runHook preInstall
                          mkdir -p $out
                          touch $out/pytest-ok
                          runHook postInstall
                        '';
                      };
                    };
                };
              });
            })
          ]
        )
      );
    in
    {
      # Runtime virtualenv — what `nix build .#default` produces.
      # `nix run .#mcp-silverbullet` (and `nix run .`) executes the
      # console script inside that venv.
      packages = forAllSystems (
        system:
        let
          venv = pythonSets.${system}.mkVirtualEnv "mcp-silverbullet-env" workspace.deps.default;
        in
        {
          default = venv;
          mcp-silverbullet = venv;
        }
      );

      apps = forAllSystems (
        system:
        let
          app = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/mcp-silverbullet";
          };
        in
        {
          default = app;
          mcp-silverbullet = app;
        }
      );

      # Development shell. Uses the editable overlay so the source tree
      # is live — same model as the hello-world template.
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = pythonSet.mkVirtualEnv "mcp-silverbullet-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );

      # CI checks. Only `pytest` for now; expand per ticket when other
      # surfaces need a check (lint, format, etc.).
      checks = forAllSystems (system: {
        inherit (pythonSets.${system}."mcp-silverbullet".passthru.tests) pytest;
      });
    };
}
