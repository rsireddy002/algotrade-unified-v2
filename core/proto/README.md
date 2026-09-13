# core/proto/

`market_data_feed_v3.proto` — Upstox's V3 market data feed schema, sourced
from a public Rust SDK crate that embeds it verbatim (Upstox doesn't
publish a stable direct-download link for this file, and it does change
periodically — the community has hit "urgent: proto file updated" style
announcements before, so if decoding ever starts failing, re-check this
file is still current before assuming the code is broken).

`market_data_feed_v3_pb2.py` — the compiled Python bindings, already
generated and committed so you don't need `protoc` installed to use this.

To regenerate after a schema change:

```bash
pip install grpcio-tools
python -m grpc_tools.protoc --python_out=. --proto_path=. market_data_feed_v3.proto
```
