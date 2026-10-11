from __future__ import annotations

import json
import unittest

import httpx

from astrbot_plugin_alice_agent.adapters.vad import HTTPVADModelAdapter, VADResponseError


class VADAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_text_and_preserves_raw_and_normalized_scores(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "text": "hello",
                "vad": [4.0, 3.0, 5.0],
                "valence": 0.7,
                "arousal": 0.5,
                "dominance": 1.0,
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = HTTPVADModelAdapter("http://vad.local", client=client)
        score = await adapter.score("hello")

        self.assertEqual(requests[0].url.path, "/vad")
        self.assertEqual(requests[0].content, b'{"text":"hello"}')
        self.assertEqual(score.raw, (4.0, 3.0, 5.0))
        self.assertEqual(score.normalized, (0.75, 0.5, 1.0))
        self.assertEqual(score.reported_axes["valence"], 0.7)
        await client.aclose()

    async def test_rejects_nonfinite_and_out_of_range_vectors(self) -> None:
        for vector in ([3.0, float("nan"), 4.0], [0.0, 3.0, 4.0]):
            async def handler(request: httpx.Request, vector=vector) -> httpx.Response:
                body = json.dumps({"vad": vector}, allow_nan=True).encode("utf-8")
                return httpx.Response(200, content=body)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            adapter = HTTPVADModelAdapter("http://vad.local", client=client)
            try:
                with self.assertRaises(VADResponseError):
                    await adapter.score("hello")
            finally:
                await client.aclose()


if __name__ == "__main__":
    unittest.main()
