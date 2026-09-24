"""Install the pinned, SHA-256 checked Python WASI guest (no admin/reboot)."""
from hashlib import sha256
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.code_execution import WASM_PATH, WASM_SHA256, WASM_URL


def main():
    if WASM_PATH.exists() and sha256(WASM_PATH.read_bytes()).hexdigest() == WASM_SHA256:
        print("Python WASI ready:", WASM_PATH)
        return
    with urllib.request.urlopen(WASM_URL, timeout=60) as response:
        binary = response.read(32 * 1024 * 1024)
    if sha256(binary).hexdigest() != WASM_SHA256:
        raise ValueError("Python WASI download failed SHA-256 verification")
    WASM_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = WASM_PATH.with_suffix(".download")
    temporary.write_bytes(binary)
    temporary.replace(WASM_PATH)
    print("Python WASI installed:", WASM_PATH)


if __name__ == "__main__":
    main()
