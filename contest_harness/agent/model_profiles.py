"""Approved model names and bounds for proxy requests."""



REQUEST_TIMEOUT = 120
PROFILES = {
    # Generation cap, extra reasoning allowance, chat template allowance.
    "qwen3.8-27b": (4096, 4096, 512),
    "qwen3.8-27b-noreason": (2048, 0, 256),
    "qwen3.6-35b-a3b": (4096, 4096, 512),
    "qwen3.6-35b-a3b-noreason": (2048, 0, 256),
}
