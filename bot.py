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
HP_RECOVERY_PER_MIN = 1.0
RARITY_NAMES = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic"]
FULL_THRESHOLD = 1

cached_token = None
start_time = None
start_amethyst = None

def max_energy(hero):
    return ENERGY_CAP[hero.get("rarity", 0)]

def max_hp(hero):
    return hero["dur"] * 12 + 30

def energy_pct(hero):
    return 100 * hero["energy"] / max_energy(hero)

def hp_pct(hero):
    return 100 * hero["durability"] / max_hp(hero)

def is_full(hero):
    e_full = hero["energy"] >= max_energy(hero) - FULL_THRESHOLD
    hp_full = hero["durability"] >= max_hp(hero) - FULL_THRESHOLD
    return e_full and hp_full

def time_to_full_seconds(hero):
    rarity = hero.get("rarity", 0)
    e_max = max_energy(hero)
    e_remaining = e_max - hero["energy"]
    e_rate = e_max / ENERGY_RECOVERY_MINUTES[rarity]
    e_time = (e_remaining / e_rate) * 60 if e_rate > 0 and e_remaining > 0 else 0

    hp_remaining = max_hp(hero) - hero["durability"]
    hp_time = (hp_remaining / HP_RECOVERY_PER_MIN) * 60 if hp_remaining > 0 else 0

    return max(e_time, hp_time)

def rarity_name(r):
    return RARITY_NAMES[r] if r < len(RARITY_NAMES) else f"R{r}"

def fmt_amount(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def load_config():
    import os
    if os.environ.get("PRIVATE_KEY"):
        return {
            "private_key": os.environ["PRIVATE_KEY"],
            "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL", "30")),
        }
    try:
        with open("config.json") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit("Set PRIVATE_KEY env var or create config.json")

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
    log(f"  Authenticating wallet {address[:4]}...{address[-4:]}")

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
    log("  [OK] Authenticated!")
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
        return "READY"
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h = m // 60
        m = m % 60
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s" if m else f"{s}s"

def run():
    global start_time, start_amethyst
    cfg = load_config()
    poll_interval = cfg.get("poll_interval_seconds", 30)

    print("", flush=True)
    print("========================================", flush=True)
    print("     Wizzard.fun -- Auto-Farm Bot       ", flush=True)
    print("========================================", flush=True)
    print("  Auto-login | 100% energy | Amethyst collect", flush=True)
    print("", flush=True)

    consecutive_errors = 0

    while True:
        try:
            token = get_token(cfg)
            state = api_post("state", token)

            if state.get("error") in ("HTTP 401: ", "HTTP 401: {}", "unauthorized"):
                log("  Token expired, re-authenticating...")
                global cached_token
                cached_token = None
                token = authenticate(cfg)
                state = api_post("state", token)

            if "error" in state:
                consecutive_errors += 1
                log(f"  ERROR: {state['error']}")
                if consecutive_errors > 10:
                    log("  Too many errors. Stopping.")
                    break
                time.sleep(min(poll_interval * consecutive_errors, 300))
                continue

            consecutive_errors = 0
            player = state["player"]
            heroes = player["heroes"]
            online = state.get("online", "?")
            amethyst = player.get("amethyst", 0)
            chest = player.get("amethystChest", 0)
            total_farmed = player.get("totalFarmed", 0)
            best_floor = player.get("bestFloor", 0)
            kills = player.get("totalKills", 0)

            if start_time is None:
                start_time = time.time()
                start_amethyst = amethyst + chest

            elapsed_hours = (time.time() - start_time) / 3600
            earned_session = (amethyst + chest) - start_amethyst
            daily_rate = (earned_session / elapsed_hours * 24) if elapsed_hours > 0.01 else 0

            fallen_ids = []
            ready_heroes = []
            working_heroes = []
            resting_heroes = []

            for h in heroes:
                if h.get("benched", False):
                    continue
                status = h["status"]
                if status == "fallen":
                    fallen_ids.append(h)
                elif status == "resting":
                    if is_full(h):
                        ready_heroes.append(h)
                    else:
                        resting_heroes.append(h)
                elif status == "working":
                    working_heroes.append(h)
                elif status == "idle":
                    if is_full(h):
                        ready_heroes.append(h)

            total_active = len(working_heroes) + len(ready_heroes) + len(resting_heroes) + len(fallen_ids)

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking heroes...", flush=True)
            print(f"  {total_active} heroes -- {len(working_heroes)} working, {len(resting_heroes) + len(ready_heroes)} resting, {len(fallen_ids)} fallen", flush=True)
            print(f"  Balance: {fmt_amount(amethyst)} | Chest: {fmt_amount(chest)} | Total farmed: {fmt_amount(total_farmed)}", flush=True)
            print(f"  Best floor: {best_floor} | Kills: {kills:,} | Online: {online}", flush=True)
            if elapsed_hours > 0.01:
                print(f"  Session: +{fmt_amount(earned_session)} in {format_time(elapsed_hours * 3600)} | ~{fmt_amount(int(daily_rate))}/day", flush=True)

            for h in working_heroes:
                ep = energy_pct(h)
                print(f"    #{h['id']} {rarity_name(h['rarity'])} ATK:{h['atk']} -- Working (Energy:{ep:.0f}%)", flush=True)
            for h in resting_heroes:
                ep = energy_pct(h)
                hp = hp_pct(h)
                ttf = time_to_full_seconds(h)
                print(f"    #{h['id']} {rarity_name(h['rarity'])} ATK:{h['atk']} -- Energy:{ep:.0f}% HP:{hp:.0f}% ({format_time(ttf)} left)", flush=True)
            for h in ready_heroes:
                print(f"    #{h['id']} {rarity_name(h['rarity'])} ATK:{h['atk']} -- Energy:100% HP:100% (READY)", flush=True)
            for h in fallen_ids:
                print(f"    #{h['id']} {rarity_name(h['rarity'])} ATK:{h['atk']} -- FALLEN", flush=True)

            if fallen_ids:
                ids = [h["id"] for h in fallen_ids]
                log(f"  Resting {len(ids)} fallen heroes...")
                resp = api_post("heroes/rest", token, {"ids": ids})
                if "error" in resp:
                    log(f"    [FAIL] {resp['error']}")
                else:
                    log(f"    [OK] Rested")

            if ready_heroes:
                ids = [h["id"] for h in ready_heroes]
                print("", flush=True)
                log(f"  Sending {len(ids)} heroes to work...")
                for h in ready_heroes:
                    print(f"    -> #{h['id']} {rarity_name(h['rarity'])} ATK:{h['atk']}", flush=True)
                resp = api_post("heroes/work", token, {"ids": ids})
                if "error" in resp:
                    log(f"    [FAIL] {resp['error']}")
                else:
                    log(f"    [OK] {len(ids)} heroes sent!")

            if chest >= 2000:
                log(f"  Collecting {fmt_amount(chest)} amethyst from chest...")
                resp = api_post("amethyst/collect", token)
                if "error" in resp:
                    log(f"    [FAIL] {resp['error']}")
                else:
                    collected = resp.get("collected", resp.get("value", {}).get("collected", 0))
                    balance = resp.get("player", {}).get("amethyst", 0)
                    log(f"    [OK] +{fmt_amount(collected)} | Balance: {fmt_amount(balance)}")

            if resting_heroes and not working_heroes and not ready_heroes and not fallen_ids:
                min_ttf = min(time_to_full_seconds(h) for h in resting_heroes)
                sleep_time = min(min_ttf + 10, 600)
                log(f"  Next check in {format_time(sleep_time)}")
                time.sleep(sleep_time)
                continue

            log(f"  Next check in {format_time(poll_interval)}")

        except KeyboardInterrupt:
            log("Stopped by user")
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"  ERROR: {e}")
            if consecutive_errors > 10:
                log("  Too many errors. Stopping.")
                break

        time.sleep(poll_interval)

if __name__ == "__main__":
    run()
