import json

with open("/work/config.json", encoding="utf-8") as f:
    cfg = json.load(f)

errors = []

if set(cfg) != {"service", "environment", "listen", "retries", "enabled", "tags"}:
    errors.append(f"top-level keys mismatch: {sorted(cfg)}")

if not (isinstance(cfg.get("service"), str) and cfg["service"] == "inventory"):
    errors.append("service invalid")
if not (isinstance(cfg.get("environment"), str) and cfg["environment"] == "staging"):
    errors.append("environment invalid")

listen = cfg.get("listen")
if not isinstance(listen, dict):
    errors.append("listen not an object")
else:
    if set(listen) != {"host", "port"}:
        errors.append(f"listen keys mismatch: {sorted(listen)}")
    if not (isinstance(listen.get("host"), str) and listen["host"] == "127.0.0.1"):
        errors.append("listen.host invalid")
    port = listen.get("port")
    if not (isinstance(port, int) and not isinstance(port, bool) and port == 8137):
        errors.append("listen.port invalid")

retries = cfg.get("retries")
if not (isinstance(retries, int) and not isinstance(retries, bool) and retries == 3):
    errors.append("retries invalid")

if not (isinstance(cfg.get("enabled"), bool) and cfg["enabled"] is True):
    errors.append("enabled invalid")

tags = cfg.get("tags")
if not (isinstance(tags, list) and tags == ["internal", "read-only"]):
    errors.append("tags invalid")

print("VALID" if not errors else "INVALID: " + "; ".join(errors))
raise SystemExit(0 if not errors else 1)
