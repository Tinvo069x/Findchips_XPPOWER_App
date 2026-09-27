import json
import urllib.request

BASE = "http://127.0.0.1:8765"

with urllib.request.urlopen(BASE + "/api/health", timeout=10) as r:
    print(r.read().decode("utf-8"))

payload = json.dumps({"mpns": ["W02G-E4/51"]}).encode("utf-8")
req = urllib.request.Request(
    BASE + "/api/search",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=120) as r:
    data = json.loads(r.read().decode("utf-8"))
    rows = (data.get("results") or [{}])[0].get("rows") or []
    print("Rows:", len(rows))
    for row in rows[:10]:
        print(row.get("Authorized Distributor"), row.get("MPN_Distributor"), row.get("Stock Qty"))
