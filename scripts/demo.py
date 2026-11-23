"""Run real HTTP contention, replay and expiry against disposable synthetic stock."""

import json
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn

from stockhold.api import create_app
from stockhold.store import Database


def run_demo():
    with tempfile.TemporaryDirectory(prefix="stockhold-demo-") as temporary:
        database = Database(f"sqlite:///{Path(temporary) / 'demo.db'}")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(create_app(database), log_level="error", access_log=False)
        )
        worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)

        def request(path, payload=None, key=None):
            headers = {"Content-Type": "application/json"}
            if key is not None:
                headers["Idempotency-Key"] = key
            body = None if payload is None else json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=body, headers=headers
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=15) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        def race(sku, count, quantity, same_key=False):
            barrier = threading.Barrier(count)

            def reserve(index):
                barrier.wait(timeout=10)
                return request(
                    "/v1/reservations",
                    {"sku": sku, "quantity": quantity, "ttl_seconds": 60},
                    "shared" if same_key else f"request-{index}",
                )

            with ThreadPoolExecutor(max_workers=count) as pool:
                return list(pool.map(reserve, range(count)))

        try:
            worker.start()
            deadline = time.monotonic() + 10
            while not server.started:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Demo server did not start")
                time.sleep(0.01)
            assert request("/health") == (200, {"status": "ok"})
            for sku, quantity in (("RACE", 5), ("REPLAY", 3), ("EXPIRE", 2)):
                assert request("/v1/products", {"sku": sku, "quantity": quantity})[0] == 201
            competition = race("RACE", 16, 1)
            accepted = [body["reservation"] for status, body in competition if status == 201]
            rejected = [body for status, body in competition if status == 409]
            assert len(accepted) == 5 and len(rejected) == 11
            assert all(body["error"] == "insufficient_stock" for body in rejected)
            for held in accepted[:2]:
                assert (
                    request(f"/v1/reservations/{held['id']}/confirm", {})[1]["state"] == "confirmed"
                )
            assert (
                request(f"/v1/reservations/{accepted[2]['id']}/cancel", {})[1]["state"]
                == "cancelled"
            )
            replay = race("REPLAY", 12, 2, same_key=True)
            assert sum(status == 201 for status, _ in replay) == 1
            assert sum(status == 200 for status, _ in replay) == 11
            ids = {body["reservation"]["id"] for _, body in replay}
            assert len(ids) == 1
            assert request(f"/v1/reservations/{ids.pop()}/confirm", {})[1]["state"] == "confirmed"
            expiry = request(
                "/v1/reservations", {"sku": "EXPIRE", "quantity": 2, "ttl_seconds": 1}, "abandoned"
            )[1]["reservation"]
            deadline = time.monotonic() + 10
            while True:
                with database.engine.connect() as conn:
                    if database._now(conn) >= expiry["expires_at"]:
                        break
                if time.monotonic() > deadline:
                    raise RuntimeError("Database clock did not reach the expiry deadline")
                time.sleep(0.05)
            sweep = database.expire(100)
            assert sweep == {"examined": 1, "expired": 1, "released_units": 2}
            assert database.expire(100)["expired"] == 0
            assert request(f"/v1/reservations/{expiry['id']}/confirm", {})[0] == 409
            audit = database.audit()
            assert audit == {
                "ok": True,
                "products_checked": 3,
                "reservations_checked": 7,
                "audit_events_checked": 12,
                "violations": [],
            }
            stock = [request(f"/v1/products/{sku}")[1] for sku in ("RACE", "REPLAY", "EXPIRE")]
            assert stock == [
                {"sku": "RACE", "total": 5, "available": 1, "reserved": 2, "committed": 2},
                {"sku": "REPLAY", "total": 3, "available": 1, "reserved": 0, "committed": 2},
                {"sku": "EXPIRE", "total": 2, "available": 2, "reserved": 0, "committed": 0},
            ]
            return {
                "http_demo": "passed",
                "competing_requests": 16,
                "holds_created": 5,
                "stock_conflicts": 11,
                "same_key_requests": 12,
                "same_key_new_holds": 1,
                "same_key_replays": 11,
                "sweep": sweep,
                "inventory": stock,
                "audit": audit,
            }
        finally:
            server.should_exit = True
            if worker.ident is not None:
                worker.join(timeout=10)
            sock.close()
            database.close()
            if worker.is_alive():
                raise RuntimeError("Demo server did not stop")


if __name__ == "__main__":
    print(json.dumps(run_demo(), sort_keys=True))
