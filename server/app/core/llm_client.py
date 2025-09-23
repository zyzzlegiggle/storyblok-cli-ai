import os
import asyncio

async def call_gemini(messages, timeout=60):
    # PSEUDO: Replace with actual Gemini SDK calls.
    # Example flow:
    # - create request with messages
    # - send to Gemini
    # - await response
    # return response_text

    # For hackathon stub:
    await asyncio.sleep(1)
    return '{"project_name":"demo","files":[{"path":"src/index.tsx","content":"// sample"}]}'
