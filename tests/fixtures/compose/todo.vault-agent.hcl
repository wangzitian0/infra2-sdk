# Source: wangzitian0/infra2@e276d9a7 platform/30.todo/vault-agent.hcl (verbatim after this line)
vault {
  # VAULT_ADDR is set via container environment variable
  # vault-agent CLI automatically uses VAULT_ADDR env var when address is not specified
}
auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/vault/role_id"
      secret_id_file_path                 = "/vault/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }

  sink {
    type = "file"
    config = {
      path = "/vault/.token"
    }
  }

  exit_on_err = true
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure = true
}

template {
  source      = "/etc/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
  perms       = 0644
  # The template is generated from the service's manifest; a required key missing in
  # Vault must fail the render (visible, no container start) instead of rendering
  # `%!q(<nil>)`. Optional keys are omitted by the template itself (empty_ok).
  error_on_missing_key = true
}
