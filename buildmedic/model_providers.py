"""Official Chat Completions endpoints and provider-specific request parameters."""

import re

OFFICIAL_ENDPOINTS = {
    "deepseek": {("api.deepseek.com", ""), ("api.deepseek.com", "/v1")},
    "kimi": {("api.moonshot.cn", "/v1"), ("api.moonshot.ai", "/v1")},
    "glm": {("open.bigmodel.cn", "/api/paas/v4"), ("api.z.ai", "/api/paas/v4")},
    "openai": {("api.openai.com", "/v1")},
}


def kimi_always_thinks(model_id):
    return model_id.rsplit("/", 1)[-1].lower().startswith(("kimi-k3", "kimi-k2.7-code", "kimi-k2-thinking"))


def tools_supported(cfg):
    if cfg.model_provider in {"deepseek", "kimi", "glm"} and cfg.model_thinking != "disabled":
        return False
    return not (cfg.model_provider == "kimi" and kimi_always_thinks(cfg.model_id))


def completion_parameters(cfg):
    token_parameter = cfg.model_token_parameter
    if token_parameter == "auto":
        token_parameter = "max_completion_tokens" if cfg.model_provider == "openai" else "max_tokens"
    body = {token_parameter: cfg.max_output_tokens}
    model = cfg.model_id.rsplit("/", 1)[-1].lower()
    thinking = cfg.model_provider == "deepseek"
    if cfg.model_provider == "kimi":
        thinking = model.startswith(("kimi-k2.5", "kimi-k2.6"))
    if cfg.model_provider == "glm":
        version = re.match(r"glm-(\d+)(?:\.(\d+))?", model)
        thinking = bool(version and (int(version[1]), int(version[2] or 0)) >= (4, 5))
    if thinking:
        body["thinking"] = {"type": cfg.model_thinking}
    if cfg.model_reasoning_effort is not None:
        body["reasoning_effort"] = cfg.model_reasoning_effort
    return body
