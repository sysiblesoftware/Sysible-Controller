"""A real DNS server, small enough to assert against."""
import socket, struct, threading

class FakeDNS:
    def __init__(self, zone, rcode=0, addr="127.0.0.2"):
        self.addr = addr
        self.zone = {k.lower(): v for k, v in zone.items()}
        self.rcode = rcode
        self.asked = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # The tests bind the same address and port over and over; without
        # this the next one fails to start because the last has not let go.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((addr, 53))
        self.port = 53
        self._stop = False
        self.t = threading.Thread(target=self._serve, daemon=True); self.t.start()

    def _name(self, buf, i):
        parts = []
        while True:
            n = buf[i]
            if n == 0: return ".".join(parts), i + 1
            if n & 0xC0 == 0xC0: return ".".join(parts), i + 2
            parts.append(buf[i+1:i+1+n].decode()); i += 1 + n

    def _serve(self):
        self.sock.settimeout(0.3)
        while not self._stop:
            try: buf, addr = self.sock.recvfrom(4096)
            except socket.timeout: continue
            except OSError: return
            qid = struct.unpack(">H", buf[:2])[0]
            name, i = self._name(buf, 12)
            self.asked.append(name)
            ips = self.zone.get(name.lower(), [])
            ans = b""
            for ip in ips:
                ans += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) \
                       + bytes(int(o) for o in ip.split("."))
            head = struct.pack(">HHHHHH", qid, 0x8180 | self.rcode, 1, len(ips), 0, 0)
            q = buf[12:i+4]
            self.sock.sendto(head + q + ans, addr)

    def close(self):
        self._stop = True
        self.t.join(timeout=2)      # let the loop see the flag before the close
        try:
            self.sock.close()
        except OSError:
            pass
