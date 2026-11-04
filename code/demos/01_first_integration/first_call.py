"""
Demo 1 -- Your First AI Integration

One model call. Everything else in this talk is built on these few lines.

This script builds the client by hand, on purpose, so you can see every piece.
The help desk does the same thing in helpdesk/ai/llm.py.

Run:  uv run python demos/01_first_integration/first_call.py
      uv run python demos/01_first_integration/first_call.py --temperature
"""

import argparse
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from openai import BadRequestError, OpenAI

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# https://<resource>.openai.azure.com/openai/v1/ -- the resource, not a project.
ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]

# The name you gave the deployment, which is not necessarily the model's name.
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]

TICKET = "My back wheel started clicking whenever I coast. The bike is a month old. Should I keep riding it?"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temperature", action="store_true", help="try the parameter everyone remembers")
    args = parser.parse_args()

    # No key: `az login` supplies the identity, and the token provider hands the
    # SDK a fresh token whenever it needs one.
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = OpenAI(base_url=ENDPOINT, api_key=token_provider)

    request = dict(
        model=DEPLOYMENT,
        instructions="You are a support agent for an online bike shop. Answer in two sentences.",
        input=TICKET,
        # The controls that matter now:
        reasoning={"effort": "low"},  # how hard to think: the quality/latency/cost dial
        max_output_tokens=2000,  # a ceiling, and reasoning tokens count against it
    )

    if args.temperature:
        try:
            client.responses.create(**request, temperature=0.2)
            print("This deployment accepted temperature. Most reasoning models do not.")
        except BadRequestError as exc:
            print(f"HTTP {exc.status_code}: {exc.body.get('message') if isinstance(exc.body, dict) else exc}")
            print("\nReasoning models drop sampling parameters. Reasoning effort is the dial now.")
        return

    response = client.responses.create(**request)

    print(f"\n{response.output_text}\n")
    usage = response.usage
    print(f"Model: {response.model}  |  status: {response.status}")
    print(
        f"Tokens: {usage.input_tokens} in, {usage.output_tokens} out "
        f"({usage.output_tokens_details.reasoning_tokens} of those were reasoning)"
    )


if __name__ == "__main__":
    main()
