{ self }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.rag-ai;
  schema = ../scripts/postgres-schema.sql;
  database = config.home.username;
  postgres = pkgs.postgresql_17.withPackages (p: [ p.pgvector ]);
  dataDir = "${config.xdg.dataHome}/rag-ai/postgres";
  # %t is $XDG_RUNTIME_DIR, expanded by systemd.
  socketDir = "%t/rag-ai-postgres";
  ollama = config.services.ollama;

  initdb = pkgs.writeShellScript "rag-ai-postgres-init" ''
    set -eu
    if [ ! -s ${lib.escapeShellArg dataDir}/PG_VERSION ]; then
      mkdir -p ${lib.escapeShellArg dataDir}
      chmod 700 ${lib.escapeShellArg dataDir}
      ${postgres}/bin/initdb -D ${lib.escapeShellArg dataDir} \
        --username=${lib.escapeShellArg database} \
        --auth-local=peer --auth-host=reject \
        --encoding=UTF8 --no-locale
    fi
  '';

  initSchema = pkgs.writeShellScript "rag-ai-schema" ''
    set -eu
    psql="${postgres}/bin/psql -X --set=ON_ERROR_STOP=1"
    if [ -z "$($psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${database}'")" ]; then
      ${postgres}/bin/createdb ${lib.escapeShellArg database}
    fi
    $psql -d ${lib.escapeShellArg database} -c 'CREATE EXTENSION IF NOT EXISTS vector'
    $psql -d ${lib.escapeShellArg database} -f ${schema}
  '';
in
{
  options.services.rag-ai = {
    enable = lib.mkEnableOption "rag-ai semantic retrieval";
    docsDir = lib.mkOption { type = lib.types.str; };
    docsIgnorePatterns = lib.mkOption {
      type = lib.types.nullOr (lib.types.listOf lib.types.str);
      default = null;
      description = "Ignore patterns that replace the application's defaults. Null uses the defaults.";
    };
    postgresUrl = lib.mkOption {
      type = lib.types.str;
      default = "postgresql:///${config.home.username}?host=/run/postgresql";
    };
    ollamaUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://127.0.0.1:11434/v1";
    };
    port = lib.mkOption { type = lib.types.port; default = 8000; };
    target = lib.mkOption {
      type = lib.types.str;
      default = "wiki-agents.target";
      description = "User target that starts and stops rag-ai.";
    };
    userBacking.enable = lib.mkEnableOption ''
      Postgres with pgvector and Ollama as user services, for hosts without the NixOS
      module. Postgres listens only on a Unix socket under $XDG_RUNTIME_DIR and keeps
      its data in $XDG_DATA_HOME/rag-ai/postgres
    '';
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      systemd.user.services.rag-ai = {
        Unit = {
          Description = "rag-ai semantic retrieval";
          PartOf = [ cfg.target ];
        };
        Service = {
          ExecStart = "${self.packages.${pkgs.stdenv.hostPlatform.system}.rag-ai}/bin/rag-ai";
          Environment = [
            "ENVIRONMENT=production"
            "RAG_AI_PORT=${toString cfg.port}"
            "DOCS_DIR=${cfg.docsDir}"
            "POSTGRES_URL=${cfg.postgresUrl}"
            "LLM_BASE_URL=${cfg.ollamaUrl}"
            "RAG_AI_ENV_FILE="
          ] ++ lib.optional (cfg.docsIgnorePatterns != null) (builtins.toJSON (
            "DOCS_IGNORE_PATTERNS=${lib.replaceStrings [ "%" ] [ "%%" ] (builtins.toJSON cfg.docsIgnorePatterns)}"
          ));
          Restart = "on-failure";
          RestartSec = 2;
        };
        Install.WantedBy = [ cfg.target ];
      };
    }

    (lib.mkIf cfg.userBacking.enable {
      services.rag-ai = {
        postgresUrl = lib.mkDefault "postgresql:///${database}?host=${socketDir}";
        ollamaUrl = lib.mkDefault "http://${ollama.host}:${toString ollama.port}/v1";
      };

      services.ollama.enable = true;

      systemd.user.services.rag-ai-postgres = {
        Unit = {
          Description = "Postgres with pgvector for rag-ai";
          PartOf = [ cfg.target ];
        };
        Service = {
          Type = "notify";
          RuntimeDirectory = "rag-ai-postgres";
          ExecStartPre = "${initdb}";
          ExecStart = "${postgres}/bin/postgres -D ${dataDir} -k ${socketDir} -c listen_addresses=";
          KillSignal = "SIGINT";
          KillMode = "mixed";
          TimeoutSec = 120;
          Restart = "on-failure";
        };
        Install.WantedBy = [ cfg.target ];
      };

      systemd.user.services.rag-ai-schema = {
        Unit = {
          Description = "Initialize the rag-ai pgvector schema";
          PartOf = [ cfg.target ];
          Requires = [ "rag-ai-postgres.service" ];
          After = [ "rag-ai-postgres.service" ];
          X-Restart-Triggers = [ "${schema}" ];
        };
        Service = {
          Type = "oneshot";
          RemainAfterExit = true;
          Environment = [ "PGHOST=${socketDir}" ];
          ExecStart = "${initSchema}";
        };
        Install.WantedBy = [ cfg.target ];
      };

      systemd.user.services.rag-ai-ollama-model = {
        Unit = {
          Description = "Pull the rag-ai embedding model into Ollama";
          PartOf = [ cfg.target ];
          Requires = [ "ollama.service" ];
          After = [ "ollama.service" ];
        };
        Service = {
          Type = "oneshot";
          RemainAfterExit = true;
          Environment = [ "OLLAMA_HOST=${ollama.host}:${toString ollama.port}" ];
          ExecStart = "${lib.getExe ollama.package} pull nomic-embed-text";
          # Ollama may still be starting.
          Restart = "on-failure";
          RestartSec = 5;
        };
        Install.WantedBy = [ cfg.target ];
      };

      systemd.user.services.rag-ai.Unit = {
        Wants = [ "rag-ai-schema.service" "rag-ai-ollama-model.service" ];
        After = [ "rag-ai-schema.service" "rag-ai-ollama-model.service" ];
      };
    })
  ]);
}
