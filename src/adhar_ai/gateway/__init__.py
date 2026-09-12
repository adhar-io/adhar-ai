"""Provider-agnostic LLM gateway (ADR-0024 §4).

One secret (`adhar-ai-llm`) selects the backend. The API key never leaves this
process: callers speak the OpenAI-compatible wire format and the gateway
normalizes tool-calling across providers.
"""
