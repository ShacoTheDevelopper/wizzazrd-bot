import json
import time
import base64
import urllib.request
import urllib.error
from datetime import datetime

import nacl.signing

BASE_URL = "https://wizzard.fun/api"

ENERGY_CAP = [100, 150, 220, 300, 400, 500]
ENERGY_RECOVERY_MINUTES = [20, 25, 30, 35, 40, 45]
FULL_THRESHOLD = 1

cached_token = None

def max_energy(hero):
    return ENERGY_CAP[hero.get("rarity", 0)]

def hero_pct(hero):
    return 100 * hero["energy"] / max_energy(hero)

def is_full(hero):
    return hero["energy"] >= max_energy(hero) - FULL_THRESHOLD

def time_to_full_seconds(hero):
    rarity = hero.get("rarity", 0)
    e_max = max_energy(hero)
    remaining = e_max - hero["energy"]
    rate = e_max / ENERGY_RECOVERY_MINUTES[rarity]
    return (remaining / rate) * 60 if rate > 0 and remaining > 0 else 0

def load_config():
    import os
    if os.environ.get("PRIVATE_KEY"):
        return {
            "private_key": os.environ["PRIVATE_KEY"],
            "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL", "30")),
        }
    with open("config.json") as f:
        return json.load(f)

def get_keypair(private_key_base58):
    import base58
    secret = base58.b58decode(private_key_base58)
    return nacl.signing.SigningKey(secret[:32])

def get_address(signing_key):
    import base58
    return base58.b58encode(bytes(signing_key.verify_key)).decode()

def authenticate(cfg):
    global cached_token
    signing_key = get_keypair(cfg["private_key"])
    address = get_address(signing_key)
    log(f"Authenticating wallet {address[:4]}...{address[-4:]}")

    challenge = api_post_raw("wallet/challenge")
    if "error" in challenge:
        raise Exception(f"Challenge failed: {challenge['error']}")

    nonce = challenge["nonce"]
    message = challenge["message"].replace("<address>", address)
    msg_bytes = message.encode("utf-8")
    signed = signing_key.sign(msg_bytes)
    signature = signed.signature

    body = {
        "nonce": nonce,
        "address": address,
        "signedMessage": base64.b64encode(msg_bytes).decode(),
        "signature": base64.b64encode(signature).decode(),
    }

    resp = api_post_raw("wallet/sign-in", body)
    if "error" in resp:
        raise Exception(f"Sign-in failed: {resp['error']}")

    token = resp.get("token")
    if not token:
        raise Exception("Sign-in response missing token")

    cached_token = token
    log("Authenticated OK")
    return token

def get_token(cfg):
    global cached_token
    if cached_token:
        return cached_token
    return authenticate(cfg)

def build_headers(token):
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
    }

def api_post_raw(endpoint, body=None):
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(f"{BASE_URL}/{endpoint}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return json.loads(body_text)
        except Exception:
            return {"error": f"HTTP {e.code}: {body_text[:200]}"}
    except Exception as e:
        return {"error": str(e)}

def api_post(endpoint, token, body=None):
    data = json.dumps(body or {}).encode()
    headers = build_headers(token)
    req = urllib.request.Request(f"{BASE_URL}/{endpoint}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return json.loads(body_text)
        except Exception:
            return {"error": f"HTTP {e.code}: {body_text[:200]}"}
    except Exception as e:
        return {"error": str(e)}

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def format_time(seconds):
    if seconds <= 0:
        return "ready"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"

def run():
    cfg = load_config()
    poll_interval = cfg.get("poll_interval_seconds", 30)

    log("Wizzard bot started (auto-login, waits for 100% energy)")
    log(f"Polling every {poll_interval}s")

    consecutive_errors = 0

    while True:
        try:
            token = get_token(cfg)
            state = api_post("state", token)

            if state.get("error") == "HTTP 401: ":
                log("Token expired, re-authenticating...")
                global cached_token
                cached_token = None
                token = authenticate(cfg)
                state = api_post("state", token)

            if "error" in state:
                consecutive_errors += 1
                log(f"API error: {state['error']}")
                if consecutive_errors > 10:
                    log("Too many errors. Stopping.")
                    break
                time.sleep(min(poll_interval * consecutive_errors, 300))
                continue

            consecutive_errors = 0
            heroes = state["player"]["heroes"]
            online = state.get("online", "?")

            fallen_ids = []
            ready_ids = []
            working = []
            recovering = []

            for h in heroes:
                hid = h["id"]
                status = h["status"]
                if h.get("benched", False):
                    continue

                if status == "fallen":
                    fallen_ids.append(hid)
                elif status == "resting":
                    pct = hero_pct(h)
                    if is_full(h):
                        ready_ids.append(hid)
                    else:
                        ttf = time_to_full_seconds(h)
                        recovering.append((hid, pct, ttf))
                elif status == "working":
                    working.append((hid, hero_pct(h)))
                elif status == "idle":
                    if is_full(h):
                        ready_ids.append(hid)

            parts = [f"Online: {online}"]
            if working:
                parts.append(f"Working: {[(hid, f'{p:.0f}%') for hid, p in working]}")
            if ready_ids:
                parts.append(f"Ready(100%): {ready_ids}")
            if fallen_ids:
                parts.append(f"Fallen: {fallen_ids}")
            if recovering:
                parts.append(f"Recovering: {[(hid, f'{p:.0f}%', format_time(t)) for hid, p, t in recovering]}")
            log(" | ".join(parts))

            if fallen_ids:
                log(f"  -> Resting fallen heroes: {fallen_ids}")
                resp = api_post("heroes/rest", token, {"ids": fallen_ids})
                if "error" in resp:
                    log(f"     Rest error: {resp['error']}")
                else:
                    log(f"     Rested OK")

            if ready_ids:
                log(f"  -> Sending 100% heroes to work: {ready_ids}")
                resp = api_post("heroes/work", token, {"ids": ready_ids})
                if "error" in resp:
                    log(f"     Work error: {resp['error']}")
                else:
                    log(f"     Sent to work OK")

            chest = state["player"].get("amethystChest", 0)
            if chest >= 2000:
                log(f"  -> Collecting {chest:,} amethyst from chest")
                resp = api_post("amethyst/collect", token)
                if "error" in resp:
                    log(f"     Collect error: {resp['error']}")
                else:
                    collected = resp.get("collected", resp.get("value", {}).get("collected", "?"))
                    balance = resp.get("player", {}).get("amethyst", "?")
                    log(f"     Collected {collected:,} | Balance: {balance:,}")

            if recovering and not working and not ready_ids and not fallen_ids:
                min_ttf = min(t for _, _, t in recovering)
                sleep_time = min(min_ttf + 10, 600)
                log(f"  All recovering. Next ready in ~{format_time(min_ttf)}. Sleeping {format_time(sleep_time)}...")
                time.sleep(sleep_time)
                continue

        except KeyboardInterrupt:
            log("Stopped by user")
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"Unexpected error: {e}")
            if consecutive_errors > 10:
                log("Too many errors. Stopping.")
                break

        time.sleep(poll_interval)

if __name__ == "__main__":
    run()
