"""Separate secretary model instance; no env loading or shared configuration mutation."""
from ._accepted_client import ModelClient

def deepseek_model(api_key):
    if not isinstance(api_key,str) or not api_key.strip():
        raise ValueError("Missing private DS key")
    return ModelClient({"base_url":"https://api.deepseek.com", "api_key":api_key,
                        "model":"deepseek-flash", "experiment_thinking":"low"})
