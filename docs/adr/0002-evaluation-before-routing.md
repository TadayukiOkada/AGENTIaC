# ADR 0002: Capability-gated model routing

Status: accepted.

Coding success and tool reliability have similar weight. Choose explicit capability aliases and staged tests before production promotion. Start with llama.cpp as the common measured baseline, keep vLLM/SGLang/TRT-LLM as justified comparisons. Reject silent fallback from Spark to a weaker Orin model. Reject relying on publisher coding scores alone or synthetic context filling. No network KV cache/distributed model due to limited links.

Consequence: backend/model profiles may differ by worker; raw performance is insufficient for promotion. 128K contexts and concurrency need separate evidence. Gateway availability errors are preferable to hidden capability changes.
