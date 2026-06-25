import time
import socket
import requests
import statistics
import nacl.bindings
import nacl.utils
from nacl.signing import SigningKey

# ==========================================
# CONFIGURATION
# ==========================================
TARGET_DOMAIN = "google.com"
GATEWAY_IP = "127.0.0.1"
GATEWAY_PORT = 8888
ITERATIONS = 20

# Shared Key (Must match Gateway)
SHARED_KEY = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f' \
             b'\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f'

# Generate a temporary identity for this benchmark client
client_seed = nacl.utils.random(32)
client_sign_key = SigningKey(client_seed)
client_pub_key = client_sign_key.verify_key.encode()

def benchmark_standard_dns():
    times = []
    for _ in range(ITERATIONS):
        start = time.time()
        try:
            socket.gethostbyname(TARGET_DOMAIN)
            times.append((time.time() - start) * 1000)
        except:
            pass
    return times

def benchmark_doh():
    # Using Google DNS over HTTPS
    url = f"https://dns.google/resolve?name={TARGET_DOMAIN}"
    times = []
    for _ in range(ITERATIONS):
        start = time.time()
        try:
            requests.get(url, timeout=5)
            times.append((time.time() - start) * 1000)
        except:
            pass
    return times

def benchmark_custom_protocol():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    times = []
    
    for _ in range(ITERATIONS):
        # 1. Construct Packet
        nonce = nacl.utils.random(12)
        timestamp = int(time.time())
        ts_bytes = struct.pack('<Q', timestamp)
        
        header = client_pub_key + nonce + ts_bytes
        
        # Encrypt
        payload = TARGET_DOMAIN.encode()
        encrypted = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
            payload, header, nonce, SHARED_KEY
        )
        # encrypted has ciphertext + tag
        
        # Sign
        to_sign = header + encrypted
        signature = client_sign_key.sign(to_sign).signature
        
        packet = to_sign + signature
        
        start = time.time()
        try:
            sock.sendto(packet, (GATEWAY_IP, GATEWAY_PORT))
            data, _ = sock.recvfrom(1024)
            times.append((time.time() - start) * 1000)
        except socket.timeout:
            print("Timeout")
        except Exception as e:
            print(e)
            
    return times

import struct

def print_stats(name, times):
    if not times:
        print(f"{name}: Failed")
        return
    avg = statistics.mean(times)
    med = statistics.median(times)
    stdev = statistics.stdev(times) if len(times) > 1 else 0
    print(f"{name:20} | Avg: {avg:6.2f}ms | Median: {med:6.2f}ms | Stdev: {stdev:6.2f}ms")

if __name__ == "__main__":
    print(f"Benchmarking DNS Protocols (Target: {TARGET_DOMAIN}, Iterations: {ITERATIONS})")
    print("-" * 70)
    
    # 1. Standard DNS
    print("Running Standard DNS (UDP)...")
    t_dns = benchmark_standard_dns()
    
    # 2. DoH
    print("Running DNS over HTTPS (DoH)...")
    t_doh = benchmark_doh()
    
    # 3. Custom Protocol
    print("Running Custom Secure Protocol...")
    # Note: Ensure gateway/server.py is running!
    t_custom = benchmark_custom_protocol()
    
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print_stats("Standard DNS (UDP)", t_dns)
    print_stats("DNS over HTTPS", t_doh)
    print_stats("Custom Secure DNS", t_custom)
    print("=" * 70)
