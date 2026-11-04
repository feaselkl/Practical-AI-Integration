"""
The one place that knows which model provider we are talking to.

Azure OpenAI and Ollama both speak the OpenAI API, so both are a plain
`OpenAI` client. The only differences are the base URL and the credential.
That's the whole "cloud or local" story: one setting in .env.
"""

from functools import cache

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI

from helpdesk.config import settings

AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"


def _azure_client() -> OpenAI:
    if not settings.azure_endpoint or "your-resource" in settings.azure_endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not set. Run infra/setup.sh or set LLM_PROVIDER=ollama.")

    # Entra ID by default: a token provider in place of a key. The SDK calls it
    # for a fresh token whenever it needs one.
    api_key = settings.azure_api_key or get_bearer_token_provider(DefaultAzureCredential(), AZURE_SCOPE)
    return OpenAI(
        base_url=settings.azure_endpoint,
        api_key=api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def _ollama_client() -> OpenAI:
    # Ollama ignores the key, but the SDK insists on having one.
    return OpenAI(
        base_url=settings.ollama_base_url,
        api_key="ollama",
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


@cache
def chat_client() -> OpenAI:
    return _azure_client() if settings.llm_provider == "azure" else _ollama_client()


def chat_model() -> str:
    # On Azure, `model` is the deployment name, not the model name.
    return settings.azure_deployment if settings.llm_provider == "azure" else settings.ollama_chat_model


def reasoning_options() -> dict:
    """
    Reasoning effort: the control that replaced temperature on reasoning models.
    It trades thinking time for quality. Triage doesn't need much thought, and on
    a laptop GPU a thinking model at full effort takes minutes per ticket.
    """
    effort = settings.reasoning_effort if settings.llm_provider == "azure" else settings.ollama_reasoning_effort
    return {"reasoning": {"effort": effort}} if effort else {}


@cache
def embedding_client() -> OpenAI:
    return _azure_client() if settings.embedding_provider == "azure" else _ollama_client()


def embedding_model() -> str:
    if settings.embedding_provider == "azure":
        return settings.azure_embedding_deployment
    return settings.ollama_embedding_model


def describe() -> str:
    """One line for logs and the UI footer: what is actually answering."""
    return f"chat: {settings.llm_provider}/{chat_model()} | embeddings: {settings.embedding_provider}/{embedding_model()}"
