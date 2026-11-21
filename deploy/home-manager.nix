{ self }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.rag-ai;
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
  };

  config = lib.mkIf cfg.enable {
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
  };
}
