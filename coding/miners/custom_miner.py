from coding.protocol import StreamCodeSynapse
from typing import Awaitable
import httpx
import os
from functools import partial

CODE_ENDPOINT = os.environ.get("CODE_ENDPOINT", "http://localhost:8000/api")

async def miner_process(self, synapse: StreamCodeSynapse) -> Awaitable:
    """
    The miner process function is called every time the miner receives a request. This function should contain the main logic of the miner.
    """
    if synapse.files:
        files=str(synapse.files)
    input={
        "query": synapse.query,
        "files": files,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=60)) as client:
            response = await client.post(CODE_ENDPOINT, json=input)
            response = response.json()
    except Exception as e:
        print(e)
        response = {
            "response": synapse.prompt * 10,
        }
    async def _forward(response, send):
        await send(
            {
                "type": "http.response.body",
                "body": response["response"].encode("utf-8"),
                "more_body": False,
            }
        )
    token_streamer = partial(_forward, response)      

    return synapse.create_streaming_response(token_streamer)
    