{ config, lib, pkgs, ... }:
let
  cfg = config.services.rag-ai.backing;
  database = lib.escapeShellArg cfg.user;
  psql = "${config.services.postgresql.package}/bin/psql -X --set=ON_ERROR_STOP=1 --host=/run/postgresql --port=${toString config.services.postgresql.settings.port} --dbname=${database}";
  schema = ../scripts/postgres-schema.sql;
in
{
  options.services.rag-ai.backing = {
    enable = lib.mkEnableOption "local Postgres with pgvector and Ollama for rag-ai";
    user = lib.mkOption {
      type = lib.types.str;
      description = "Existing Unix account for rag-ai. Also the Postgres role and database name.";
    };
  };

  config = lib.mkIf cfg.enable {
    services.postgresql = {
      enable = true;
      package = pkgs.postgresql_17;
      extensions = extensions: [ extensions.pgvector ];
      ensureDatabases = [ cfg.user ];
      ensureUsers = [{ name = cfg.user; ensureDBOwnership = true; }];
    };

    services.ollama = {
      enable = true;
      loadModels = [ "nomic-embed-text" ];
    };

    users.users.${cfg.user}.linger = true;

    systemd.services.rag-ai-schema = {
      description = "Initialize the rag-ai pgvector schema";
      wantedBy = [ "multi-user.target" ];
      requires = [ "postgresql-setup.service" ];
      after = [ "postgresql-setup.service" ];
      restartTriggers = [ schema ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        ${pkgs.util-linux}/bin/runuser -u postgres -- ${psql} -c 'CREATE EXTENSION IF NOT EXISTS vector'
        ${pkgs.util-linux}/bin/runuser -u ${database} -- ${psql} -f ${schema}
      '';
    };
  };
}
